"""로그 한 줄에 붙일 수 있는 필드(LOG_SCHEMA.md 5절 "필드"). content LogField.java와 같은 이름·규칙이다.

서비스 코드는 LogField의 정적 팩토리로만 필드를 만든다. 임의의 이름·값을 받는 팩토리가 없으므로
원고·제목·프롬프트·모델 출력 같은 내용이 필드로 들어갈 자리가 없다. 새 필드가 필요하면 LOG_SCHEMA.md
필드 표에 먼저 더한 뒤 여기에 팩토리를 만든다.

필드 하나는 이름·값 쌍을 여러 개 가질 수 있다. 예를 들어 문자열 입력값은 `loresentry.param.title: "[redacted]"`와
`loresentry.param.title_length: 7` 두 쌍이다. 값이 None이면 팩토리가 None을 돌려주고, 로그 클래스가 None 필드를
건너뛰므로 그 필드는 줄에서 빠진다.
"""

import math
import os.path
import traceback
from datetime import timedelta
from enum import Enum
from urllib.parse import urlsplit
from uuid import UUID

# 가린 문자열 자리에 남기는 값. Logstash mask.rb의 표시와 같다
REDACTED = "[redacted]"
# 허용 목록 밖의 코드 값을 대신하는 값
OTHER = "other"
# 메시지 없는 스택에 남기는 원인 수와 원인마다의 프레임 수 상한
MAX_CAUSES = 5
MAX_FRAMES = 20
_PARAM = "loresentry.param."
_RESULT = "loresentry.result."


class LogField:
    """줄에 더할 이름·값 쌍 묶음. 직접 만들지 않고 아래 정적 팩토리를 쓴다.

    entries는 점 이름(`loresentry.project_id`)과 값의 tuple이다. 포매터가 점 이름을 중첩 JSON 객체로 펼친다.
    """

    __slots__ = ("entries",)

    def __init__(self, entries: tuple[tuple[str, object], ...]):
        self.entries = entries

    def __repr__(self) -> str:
        return f"LogField{self.entries!r}"

    # ------------------------------------------------------------ 도메인 ID (loresentry.*, keyword)
    # 이 줄의 대상·대상이 속한 범위·새로 만든 대상만 여기에 넣는다. UUID 또는 표준 표기 UUID 문자열만 남긴다.

    @staticmethod
    def project_id(value: UUID | str | None) -> "LogField | None":
        return _uuid_field("loresentry.project_id", value)

    @staticmethod
    def document_id(value: UUID | str | None) -> "LogField | None":
        return _uuid_field("loresentry.document_id", value)

    @staticmethod
    def file_id(value: UUID | str | None) -> "LogField | None":
        return _uuid_field("loresentry.file_id", value)

    @staticmethod
    def chat_session_id(value: UUID | str | None) -> "LogField | None":
        """ai-chat 대화 세션. 인증 세션과 헷갈리지 않게 chat 아래에 둔다."""
        return _uuid_field("loresentry.chat.session_id", value)

    @staticmethod
    def chat_turn_id(value: UUID | str | None) -> "LogField | None":
        return _uuid_field("loresentry.chat.turn_id", value)

    @staticmethod
    def setting_check_run_id(value: UUID | str | None) -> "LogField | None":
        return _uuid_field("loresentry.setting_check.run_id", value)

    @staticmethod
    def refresh_request_id(value: UUID | str | None) -> "LogField | None":
        """관계도 최신화 요청(graph-refresh requestId). gateway → content → graph-rag를 이 필드 하나로 따라간다."""
        return _uuid_field("loresentry.refresh.request_id", value)

    # ------------------------------------------------------------ 입력값·결과 (loresentry.param.*, result.*)

    @staticmethod
    def param(name: str, value: object) -> "LogField | None":
        """메서드에 들어온 값과 처리 중의 중간값. 값의 종류에 따라 바꿔 남긴다(_converted 참고)."""
        return _converted(_PARAM, name, value)

    @staticmethod
    def result(name: str, value: object) -> "LogField | None":
        """처리 결과(건수, 상태 등). param과 같은 규칙으로 바꿔 남긴다."""
        return _converted(_RESULT, name, value)

    @staticmethod
    def code(name: str, value: str | None, allowed) -> "LogField | None":
        """정해진 낱말 중 하나인 입력 코드. 허용 목록 안이면 그대로, 밖이면 other로 남긴다."""
        if value is None:
            return None
        return _of(_PARAM + name, value if value in allowed else OTHER)

    @staticmethod
    def result_code(name: str, value: str | None, allowed) -> "LogField | None":
        """정해진 낱말 중 하나인 결과 코드(상태 이름 등). 허용 목록 밖이면 other."""
        if value is None:
            return None
        return _of(_RESULT + name, value if value in allowed else OTHER)

    # ------------------------------------------------------------ 에러 (ECS)

    @staticmethod
    def error_type(value: str | None) -> "LogField | None":
        """실패 종류. 예외 클래스 이름이나 우리 이유 코드(UNKNOWN_EVENT_TYPE 등)."""
        return _of("error.type", value)

    @staticmethod
    def error_code(value: str | None) -> "LogField | None":
        """상대(AWS, OpenAI 등)가 준 오류 코드."""
        return _of("error.code", value)

    @staticmethod
    def safe_stack_trace(failure: BaseException | None) -> "LogField | None":
        """예외 메시지를 뺀 스택(`error.stack_trace`).

        원인 사슬(__cause__, 숨기지 않은 __context__)을 MAX_CAUSES개까지 따라가며 원인마다 클래스 이름 한 줄과
        프레임 MAX_FRAMES개(`\\tat 모듈.함수(파일:줄)`, 예외가 난 곳부터)를 적는다. 예외 메시지에는 쿼리·요청 값·
        원고가 섞일 수 있어 어디에도 적지 않는다.
        """
        if failure is None:
            return None
        lines: list[str] = []
        seen: set[int] = set()
        current: BaseException | None = failure
        for index in range(MAX_CAUSES):
            # 원인이 자기 자신이나 앞의 원인을 가리키면 거기서 멈춘다
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            prefix = "" if index == 0 else "Caused by: "
            lines.append(prefix + _class_name(type(current)))
            # walk_tb는 잡은 곳 → 던진 곳 순서다. 던진 곳에 가까운 프레임을 남기고 Java처럼 던진 곳부터 적는다
            frames = list(traceback.walk_tb(current.__traceback__))[-MAX_FRAMES:]
            for frame, line_number in reversed(frames):
                module = frame.f_globals.get("__name__", "?")
                file_name = os.path.basename(frame.f_code.co_filename)
                lines.append(
                    f"\tat {module}.{frame.f_code.co_qualname}({file_name}:{line_number})"
                )
            if current.__cause__ is not None:
                current = current.__cause__
            elif current.__suppress_context__:
                current = None
            else:
                current = current.__context__
        return _of("error.stack_trace", "\n".join(lines))

    # ------------------------------------------------------------ Kafka·CloudEvents (loresentry.*)
    # ECS 8.11에 메시징 필드 묶음이 없어 loresentry 아래에 둔다.

    @staticmethod
    def kafka_topic(topic: str | None) -> "LogField | None":
        return _of("loresentry.kafka.topic", topic)

    @staticmethod
    def kafka_partition(partition: int | None) -> "LogField | None":
        return _of("loresentry.kafka.partition", _int(partition))

    @staticmethod
    def kafka_offset(offset: int | None) -> "LogField | None":
        return _of("loresentry.kafka.offset", _int(offset))

    @staticmethod
    def cloudevent_id(value: UUID | str | None) -> "LogField | None":
        """이벤트 ID(ce_id). 표준 표기 UUID일 때만 남긴다(받은 헤더는 보낸 쪽 값 그대로다)."""
        return _uuid_field("loresentry.cloudevent.id", value)

    @staticmethod
    def cloudevent_type(value: str | None, known=None) -> "LogField | None":
        """이벤트 종류(ce_type).

        known을 주면 받은 메시지의 값이다. 이 서비스가 처리하는 종류면 그대로, 목록 밖이면 other로 남긴다.
        known이 없으면 우리가 보내는 이벤트 이름(GraphRefreshCompleted 등)이라 그대로 남긴다.
        """
        if value is None:
            return None
        if known is not None and value not in known:
            return _of("loresentry.cloudevent.type", OTHER)
        return _of("loresentry.cloudevent.type", value)

    @staticmethod
    def cloudevent_subject(value: UUID | str | None) -> "LogField | None":
        """이벤트 대상(ce_subject). 표준 표기 UUID일 때만 남긴다."""
        return _uuid_field("loresentry.cloudevent.subject", value)

    @staticmethod
    def schedule_name(name: str | None) -> "LogField | None":
        """스케줄·폴링 작업 이름(refresh-outbox-relay 등). 코드에 정해 둔 이름만 넘긴다."""
        return _of("loresentry.schedule.name", name)

    # ------------------------------------------------------------ HTTP (ECS·OpenTelemetry)

    @staticmethod
    def http_method(method: str | None) -> "LogField | None":
        return _of("http.request.method", method)

    @staticmethod
    def url_path(path: str | None) -> "LogField | None":
        """이 서비스가 받은 요청의 실제 경로(쿼리 제외)."""
        return _of("url.path", path)

    @staticmethod
    def route(template: str | None) -> "LogField | None":
        """경로 템플릿(/health/db 등). OpenTelemetry http.route와 같은 뜻이다."""
        return _of("http.route", template)

    @staticmethod
    def status_code(status: int | None) -> "LogField | None":
        return _of("http.response.status_code", _int(status))

    @staticmethod
    def url_domain(host: str | None) -> "LogField | None":
        """나가는 호출의 대상 호스트."""
        return _of("url.domain", host)

    @staticmethod
    def url_full(url: str | None) -> "LogField | None":
        """나가는 호출의 대상 주소. scheme://host:port/path만 남기고 쿼리·fragment·계정은 버린다."""
        if url is None:
            return None
        try:
            parts = urlsplit(str(url))
            host, port = parts.hostname, parts.port
        except ValueError:
            return None
        if not parts.scheme or not host:
            return None
        # IPv6 주소는 대괄호로 감싼다
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        return _of("url.full", f"{parts.scheme}://{netloc}{parts.path}")


def duration(value: timedelta | float | int | None) -> LogField | None:
    """걸린 시간(`event.duration`, 나노초 정수). 시간은 로깅 팩토리만 재므로 이 모듈 안에서만 쓴다.

    Args:
        value: timedelta 또는 초 단위 숫자.
    """
    if value is None:
        return None
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    return _of("event.duration", int(round(seconds * 1_000_000_000)))


def _of(name: str, value: object) -> LogField | None:
    """이름·값 쌍 하나. 값이 None이면 필드를 만들지 않는다."""
    return None if value is None else LogField(((name, value),))


def _int(value: int | None) -> int | None:
    return None if value is None else int(value)


def _uuid_text(value: object) -> str | None:
    """UUID 또는 표준 표기(36자) UUID 문자열이면 소문자 표준 표기, 아니면 None."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and len(value) == 36:
        try:
            return str(UUID(value))
        except ValueError:
            return None
    return None


def _uuid_field(name: str, value: object) -> LogField | None:
    return _of(name, _uuid_text(value))


def _converted(prefix: str, name: str, value: object) -> LogField | None:
    """LOG_SCHEMA.md 5절 "입력값과 결과" 규칙대로 값을 바꾼다.

    - bool·int·float: 그대로(bool을 int보다 먼저 본다. bool은 int의 하위 타입이다). 유한하지 않은 float는 버린다
    - Enum: 이름(.name). StrEnum·IntEnum도 값이 아니라 이름이다
    - UUID: 표준 표기 문자열
    - str: 늘 "[redacted]"와 `<이름>_length`. 제목·본문·프롬프트 같은 내용이 남지 않는다
    - bytes: `<이름>_bytes`, 컬렉션·dict: `<이름>_count`
    - 그 밖의 객체(DTO 등)와 None: 필드를 만들지 않는다. 필요한 숫자·코드만 골라 넘긴다
    """
    key = prefix + name
    if value is None:
        return None
    if isinstance(value, bool):
        return _of(key, value)
    if isinstance(value, Enum):
        return _of(key, value.name)
    if isinstance(value, int):
        return _of(key, value)
    if isinstance(value, float):
        return _of(key, value) if math.isfinite(value) else None
    if isinstance(value, UUID):
        return _of(key, str(value))
    if isinstance(value, str):
        return LogField(((key, REDACTED), (key + "_length", len(value))))
    if isinstance(value, bytes | bytearray):
        return _of(key + "_bytes", len(value))
    if isinstance(value, memoryview):
        return _of(key + "_bytes", value.nbytes)
    if isinstance(value, list | tuple | set | frozenset | dict):
        return _of(key + "_count", len(value))
    return None


def _class_name(kind: type) -> str:
    """스택 첫 줄에 쓰는 클래스 이름. 내장 예외는 이름만, 나머지는 모듈을 붙인다."""
    if kind.__module__ == "builtins":
        return kind.__qualname__
    return f"{kind.__module__}.{kind.__qualname__}"
