"""서비스 코드가 로그를 남기는 유일한 통로 AppLogger(LOG_SCHEMA.md 5절 "로그 클래스"). content AppLogger.java와 같은 API다.

서비스 코드가 쓰는 것은 `AppLogger.current()`로 꺼낸 객체의 info·warn·error, 흐름 중간에 알게 된 사용자를 알리는
identify, 결과 줄에 이유를 맡기는 add_to_outcome, 오류 응답 본문에 싣는 trace_id뿐이다. 객체를 만들고 바인딩하는
일(new_logger, bind, _derive 등)은 로깅 팩토리(RequestLogMiddleware, LogFlow, LogScope)만 한다.

흐름
1. 시작점(HTTP 요청, Kafka 레코드, 스케줄·폴링 회차, outbox 행)이 그때 아는 trace·span·user로 객체를 만든다.
2. bind로 contextvars에 넣는다. 같은 흐름의 어느 계층이든 `AppLogger.current()`로 꺼낸다. 처리 스레드·스레드 풀은
   contextvars를 옮기지 않으므로 그 스레드 안에서 시작점이 새로 바인딩한다.
3. 줄마다 찍은 위치(모듈·함수·레이어·패키지)와 필드를 LogRecord에 실어 표준 logging으로 넘긴다. root logger의
   EcsFormatter가 ECS JSON 한 줄로 만든다. 같은 문맥에서 프레임워크가 찍은 줄에도 바인딩된 객체의 trace·user·문맥
   필드가 붙는다(Java MDC와 같은 역할).

객체는 바꾸지 않는다. 나중에 아는 값은 _derive 등으로 만든 새 객체를 다시 바인딩한다. 예외는 흐름 공유 칸(Shared)
하나다. 흐름 하나에서 만든 객체들이 같은 칸을 써서, 결과 줄에 붙일 이유와 흐름 중간에 알게 된 사용자를 나눈다.
"""

import logging
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID

from app.logs._fields import LogField, _uuid_text
from app.logs._trace import current_ids

# LogRecord에 줄 정보를 싣는 속성 이름. 이 속성이 있으면 우리 줄, 없으면 프레임워크 줄이다
LINE_ATTRIBUTE = "_loresentry_line"
# 이 패키지(로깅 팩토리) 모듈. 줄의 위치를 찾을 때 건너뛴다
_PACKAGE = "app.logs"

# 모듈 이름 끝 → log.layer(LOG_SCHEMA.md 5절 "레이어와 위치"의 Python 규칙). 모듈에 LOG_LAYER가 있으면 그 값이 먼저다
_LAYER_BY_NAME = {
    "routes": "controller",
    "service": "service",
    "services": "service",
    "store": "repository",
    "repository": "repository",
    "client": "client",
    "content": "client",
    "model": "client",
    "llm": "client",
    "consumer": "consumer",
    "handlers": "consumer",
    "publisher": "publisher",
    "worker": "scheduler",
    "relay": "scheduler",
    "outbox": "scheduler",
}

# 지금 문맥에 바인딩된 객체. 없으면 DETACHED를 쓴다
_CURRENT: ContextVar["AppLogger | None"] = ContextVar("loresentry_logger", default=None)


@dataclass(frozen=True)
class Origin:
    """줄을 찍은 위치. log.logger(모듈)·log.origin.function·log.layer·log.package와 LogRecord의 파일·줄 번호다."""

    module: str
    function: str
    layer: str
    package: str
    filename: str
    lineno: int


@dataclass(frozen=True)
class Line:
    """포매터가 읽는 우리 줄 하나. 찍는 순간의 trace·user와 필드를 고정해 담는다."""

    trace_id: str | None
    span_id: str | None
    user_id: str | None
    fields: tuple[tuple[str, object], ...]
    context: tuple[tuple[str, object], ...]
    tags: tuple[str, ...]
    origin: Origin


class Shared:
    """흐름 하나(요청, Kafka 레코드, 스케줄 회차)에서 만든 객체들이 함께 쓰는 칸. 스레드 안전하다.

    Attributes:
        outcome: 결과 줄(request completed)에 붙일 필드(add_to_outcome).
        user: 흐름 중간에 알게 된 사용자(identify). 모르면 None.
        trace_id, span_id: 시작할 때 모르고 첫 줄 직전에 정해지는 trace(폴링 회차). 정해지지 않았으면 None.
    """

    __slots__ = (
        "outcome",
        "user",
        "trace_id",
        "span_id",
        "_before_first_line",
        "_lock",
    )

    def __init__(self):
        self.outcome: list[LogField] = []
        self.user: str | None = None
        self.trace_id: str | None = None
        self.span_id: str | None = None
        self._before_first_line: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def before_first_line(self, start: Callable[[], None]) -> None:
        """이 흐름의 첫 줄 직전에 한 번 부를 일을 맡긴다(폴링 회차의 늦은 시작 줄)."""
        with self._lock:
            self._before_first_line = start

    def take_before_first_line(self) -> Callable[[], None] | None:
        """맡겨 둔 일을 꺼내고 비운다. 시작 줄 자신이 다시 부르지 않게 꺼낸 뒤 바로 비운다."""
        with self._lock:
            start, self._before_first_line = self._before_first_line, None
            return start


class AppLogger:
    """줄 하나를 찍는 객체. `AppLogger.current()`로 꺼내 info·warn·error를 부른다.

    레벨은 info·warn·error만 있다. 필드는 LogField 팩토리가 만든 것만 받으므로 내용이 들어갈 자리가 없다.
    """

    __slots__ = ("_trace_id", "_span_id", "_user_id", "_context", "_shared", "_tags")

    def __init__(
        self,
        trace_id: str | None,
        span_id: str | None,
        user_id: str | None,
        context: tuple[LogField, ...],
        shared: Shared | None,
        tags: tuple[str, ...],
    ):
        # 직접 만들지 않는다. 로깅 팩토리가 new_logger·_derive 등으로 만든다
        self._trace_id = trace_id
        self._span_id = span_id
        self._user_id = user_id
        # 이 객체로 찍는 모든 줄에 붙는 필드(요청의 url.path, Kafka 레코드의 topic·offset 등)
        self._context = context
        # 흐름 공유 칸. 바인딩이 없는 기본 객체는 None이라 아무것도 쌓이지 않는다
        self._shared = shared
        # 이 객체로 찍는 모든 줄의 tags(health 등)
        self._tags = tags

    # ------------------------------------------------------------ 서비스 코드가 쓰는 것

    @staticmethod
    def current() -> "AppLogger":
        """지금 문맥에 바인딩된 객체. 없으면 trace 없는 기본 객체를 돌려주므로 None이 아니다."""
        bound = _CURRENT.get()
        return DETACHED if bound is None else bound

    @property
    def trace_id(self) -> str | None:
        """오류 응답 본문의 trace_id에 싣는 값. 없으면 None."""
        return self._effective_trace()[0]

    def info(self, message: str, *fields: LogField | None) -> None:
        """정상 동작의 진행 상황(진입, 상태 변화, 완료)."""
        self._emit(logging.INFO, message, fields, None)

    def warn(self, message: str, *fields: LogField | None) -> None:
        """처리했지만 예상 밖인 상황(재시도, 충돌, 건너뛴 이벤트). error.type을 함께 넘긴다."""
        self._emit(logging.WARNING, message, fields, None)

    def error(self, message: str, *fields: LogField | None) -> None:
        """작업 하나의 실패. error.type과 메시지 없는 스택을 함께 넘긴다."""
        self._emit(logging.ERROR, message, fields, None)

    def identify(self, user_id: UUID | str | None) -> None:
        """흐름 중간에 알게 된 사용자를 알린다. 이후 이 흐름의 모든 줄과 결과 줄에 user.id가 붙는다.

        UUID(또는 표준 표기 UUID 문자열)만 받는다. 바인딩된 흐름이 없으면 아무것도 하지 않는다.
        """
        text = _uuid_text(user_id)
        if self._shared is not None and text is not None:
            self._shared.user = text

    def add_to_outcome(self, *fields: LogField | None) -> None:
        """이 요청의 결과 줄(request completed)에 붙일 필드(error.type 등)를 맡긴다.

        예외 처리기는 실패를 따로 찍지 않고 이유만 맡긴다. 그래서 실패 요청 하나의 결과는 한 줄이다.
        """
        if self._shared is None:
            return
        self._shared.outcome.extend(field for field in fields if field is not None)

    # ------------------------------------------------------------ 로깅 팩토리만 쓰는 것

    def _derive(self, *fields: LogField | None) -> "AppLogger":
        """이 객체로 찍는 모든 줄에 붙일 필드를 더한 새 객체(Java with). 공유 칸은 그대로 함께 쓴다."""
        added = tuple(field for field in fields if field is not None)
        return AppLogger(
            self._trace_id,
            self._span_id,
            self._user_id,
            self._context + added,
            self._shared,
            self._tags,
        )

    def _tagged(self, tag: str) -> "AppLogger":
        """모든 줄에 tags 값을 더한 새 객체. 정해 둔 이름(health)만 쓴다."""
        if tag in self._tags:
            return self
        return AppLogger(
            self._trace_id,
            self._span_id,
            self._user_id,
            self._context,
            self._shared,
            self._tags + (tag,),
        )

    def _outcome_fields(self) -> tuple[LogField, ...]:
        """맡겨진 결과 필드. 결과 줄을 쓰는 RequestLogMiddleware가 읽는다."""
        return () if self._shared is None else tuple(self._shared.outcome)

    def _shared_state(self) -> Shared | None:
        return self._shared

    def _effective_trace(self) -> tuple[str | None, str | None]:
        """이 줄의 trace. 객체의 값, 공유 칸에 늦게 정해진 값, 지금 열려 있는 span 순서로 찾는다."""
        if self._trace_id is not None:
            return self._trace_id, self._span_id
        if self._shared is not None and self._shared.trace_id is not None:
            return self._shared.trace_id, self._shared.span_id
        return current_ids()

    def _effective_user(self) -> str | None:
        """흐름 중간에 알린 사용자(identify)가 있으면 그 값, 없으면 시작점이 준 값."""
        if self._shared is not None and self._shared.user is not None:
            return self._shared.user
        return self._user_id

    def _context_entries(self) -> tuple[tuple[str, object], ...]:
        return tuple(entry for field in self._context for entry in field.entries)

    def _emit(
        self,
        level: int,
        message: str,
        fields: tuple[LogField | None, ...] | list[LogField | None],
        origin: Origin | None,
    ) -> None:
        """줄 하나를 표준 logging으로 넘긴다.

        Args:
            origin: 줄의 위치. None이면 이 패키지 밖의 첫 호출 프레임이다. 로깅 팩토리의 시작·종료 줄은 그 흐름을 연
                서비스 함수의 위치를 넘긴다.
        """
        if origin is None:
            origin = caller_origin()
        # 찍은 모듈 이름의 logger로 보낸다. 그래서 모듈·패키지별 레벨 설정이 우리 줄에도 적용된다
        logger = logging.getLogger(origin.module)
        if not logger.isEnabledFor(level):
            return
        # 이 흐름의 첫 줄이면 맡겨 둔 시작 줄을 먼저 남긴다(폴링)
        if self._shared is not None:
            start = self._shared.take_before_first_line()
            if start is not None:
                start()
        trace_id, span_id = self._effective_trace()
        line = Line(
            trace_id=trace_id,
            span_id=span_id,
            user_id=self._effective_user(),
            fields=tuple(
                entry
                for field in fields
                if field is not None
                for entry in field.entries
            ),
            context=self._context_entries(),
            tags=self._tags,
            origin=origin,
        )
        record = logger.makeRecord(
            logger.name,
            level,
            origin.filename,
            origin.lineno,
            message,
            (),
            None,
            origin.function,
            {LINE_ATTRIBUTE: line},
        )
        logger.handle(record)


# 바인딩이 없을 때(기동, 스레드 시작, 프레임워크 콜백) 쓰는 객체. trace는 그때 열려 있는 span이 있으면 그 값이다
DETACHED = AppLogger(None, None, None, (), None, ())


def new_logger(
    trace_id: str | None, span_id: str | None, user_id: UUID | str | None = None
) -> AppLogger:
    """흐름 시작점의 객체. 흐름마다 새 공유 칸을 만든다."""
    return AppLogger(trace_id, span_id, _uuid_text(user_id), (), Shared(), ())


def bind(logger: AppLogger) -> Token:
    """객체를 지금 문맥에 바인딩한다. 돌려준 token을 reset에 넘겨 이전 객체로 되돌린다."""
    return _CURRENT.set(logger)


def reset(token: Token) -> None:
    _CURRENT.reset(token)


def bound() -> AppLogger | None:
    """지금 문맥에 바인딩된 객체(없으면 None). 포매터가 프레임워크 줄에 문맥을 붙일 때 쓴다."""
    return _CURRENT.get()


def caller_origin() -> Origin:
    """이 패키지 밖의 첫 호출 프레임 위치. 로깅 팩토리를 부른 서비스 함수다."""
    frame = sys._getframe(1)
    while frame is not None and _internal(frame.f_globals.get("__name__", "")):
        frame = frame.f_back
    if frame is None:
        return Origin(_PACKAGE, "unknown", "other", _PACKAGE, "", 0)
    return origin_of_frame(frame)


def origin_of_frame(frame) -> Origin:
    module = frame.f_globals.get("__name__", "unknown")
    return Origin(
        module=module,
        function=_function(frame.f_code),
        layer=_layer(module, frame.f_globals),
        package=frame.f_globals.get("__package__") or module.rpartition(".")[0],
        filename=frame.f_code.co_filename,
        lineno=frame.f_lineno,
    )


def internal_origin(module: str, function: str, layer: str) -> Origin:
    """이 패키지 안의 시작점(RequestLogMiddleware)이 찍는 줄의 위치."""
    return Origin(module, function, layer, _PACKAGE, "", 0)


def _internal(module: str) -> bool:
    return module == _PACKAGE or module.startswith(_PACKAGE + ".")


def _function(code) -> str:
    """log.origin.function. 함수 안의 함수·lambda는 그것을 감싼 바깥 함수 이름이다(Java 람다 규칙과 같다)."""
    qualified = code.co_qualname
    if ".<locals>." in qualified:
        return qualified.split(".<locals>.", 1)[0].rsplit(".", 1)[-1]
    return code.co_name


def _layer(module: str, module_globals: dict) -> str:
    """log.layer. 모듈의 LOG_LAYER 상수가 먼저이고, 없으면 모듈 이름 끝 규칙이다. 정하지 못하면 other."""
    declared = module_globals.get("LOG_LAYER")
    if isinstance(declared, str):
        return declared
    last = module.rsplit(".", 1)[-1]
    if last in _LAYER_BY_NAME:
        return _LAYER_BY_NAME[last]
    if last.endswith("_client"):
        return "client"
    return "other"
