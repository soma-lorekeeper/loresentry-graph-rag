"""흐름 하나의 시작·종료 줄을 남기는 로깅 팩토리 LogFlow와 바인딩만 하는 LogScope(LOG_SCHEMA.md 5절 "만들기").

서비스 코드는 흐름의 재료(작업 이름, Kafka 레코드의 위치·헤더, 호출 대상)만 넘기고 `with`로 연다. 로그 객체를 만들고
바인딩하는 일, 걸린 시간을 재는 일, 결과로 레벨을 정하는 일, 시작·종료 줄을 쓰는 일은 모두 여기서 한다. 할 일이
예외로 끝나도 종료 줄이 남는다. content LogFlow.java·LogScope.java와 같은 흐름 종류·문구다.

흐름 종류
- schedule: 하루 한 번 도는 스케줄 1회. 할 일이 없어도 시작·종료 줄을 남긴다. 예외는 삼키고 실패 줄을 남긴다
- polling: 짧은 주기 폴링 1회(1초 outbox relay). 그 회차에서 처음 줄이 찍힐 때 시작 줄을 먼저 남기고, 줄이
  하나도 없으면 시작·종료 줄과 span을 모두 남기지 않는다. 예외는 삼키고 실패 줄을 남긴다
- kafka_record: Kafka 레코드 하나. `<event> event received` → `<event> event processed`(loresentry.result.outcome)
- call: 외부 호출 하나. `<target> call completed` 또는 `<target> call failed`
- task: 흐름 안·밖의 별도 작업(스레드 수명 등). `<name> start` → `<name> completed` 또는 `<name> failed`

흐름 객체는 진행 중에 알게 된 값을 받는다: add(이후 모든 줄에 붙일 대상 ID), result(종료 줄의 결과 값),
outcome(Kafka 레코드의 결과), status(호출의 응답 상태), fail(예외 없이 끝난 실패의 이유).

시작·종료 줄의 위치(log.logger·log.origin.function·log.layer)는 흐름을 연 서비스 함수다. 실패 줄에는 예외 메시지를
싣지 않는다. 클래스 이름(error.type)과 메시지 없는 스택만 남긴다.
"""

import logging
import re
import time
from collections.abc import Iterable
from enum import Enum
from urllib.parse import urlsplit
from uuid import UUID

from app.logs._fields import LogField, duration
from app.logs._logger import (
    AppLogger,
    Origin,
    bind,
    caller_origin,
    new_logger,
    reset,
)
from app.logs._trace import (
    attach,
    detach,
    mark_failed,
    parse_traceparent,
    span_ids,
    start_consumer_span,
    start_root_span,
)

# 스케줄·폴링 회차의 시작·종료 줄 문구. 작업은 문구가 아니라 loresentry.schedule.name으로 가린다
SCHEDULE_START = "schedule run start"
SCHEDULE_COMPLETED = "schedule run completed"
SCHEDULE_FAILED = "schedule run failed"
# 상대가 5xx로 답한 호출의 error.type. 응답 상태 자체는 http.response.status_code에 있다
UPSTREAM_SERVER_ERROR = "UPSTREAM_SERVER_ERROR"
# 상대가 준 오류 코드(error.code)로 남겨도 되는 모양. 식별자 모양이 아니면 남기지 않는다
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


class Outcome(Enum):
    """Kafka 레코드 하나의 처리 결과. 종료 줄의 loresentry.result.outcome이다."""

    # 처리했다. 따로 알리지 않으면 이 값이다
    PROCESSED = "PROCESSED"
    # 이미 처리한 이벤트가 다시 와서 건너뛰었다(inbox 처리 기록)
    DUPLICATE = "DUPLICATE"
    # 이 consumer가 처리하지 않는 종류라 건너뛰었다
    SKIPPED = "SKIPPED"
    # 처리하지 못했다(예외, DLQ)
    FAILED = "FAILED"


class LogFlow:
    """흐름 하나. 아래 클래스 메서드로 만들어 `with`로 연다. 공통으로 흐름 객체의 값 알림 메서드를 가진다."""

    def __init__(self, origin: Origin):
        # 흐름을 연 서비스 함수의 위치. 시작·종료 줄의 위치다
        self._origin = origin
        # 걸린 시간을 재는 시작점(나노초, 흘러간 시간만 재는 시계)
        self._started = time.perf_counter_ns()
        # 종료 줄에 붙일 결과 값(result)
        self._result: list[LogField] = []
        # add가 연 바인딩. 흐름이 끝날 때 연 반대 순서로 닫는다
        self._bindings: list = []
        # Kafka 레코드의 결과. 알리지 않으면 종료 때 PROCESSED가 된다
        self._outcome: Outcome | None = None
        # 외부 호출의 응답 상태. 모르면 None
        self._status: int | None = None
        # 상대가 준 오류 코드(AWS·OpenAI). 모르면 None
        self._error_code: str | None = None
        # 예외 없이 끝난 실패의 이유(fail). 실패가 아니면 None
        self._failure_type: str | None = None

    # ------------------------------------------------------------ 흐름 종류

    @classmethod
    def schedule(cls, name: str, *start: LogField | None) -> "LogFlow":
        """하루 한 번 도는 스케줄 작업 1회. 새 trace(회차 span)와 작업 이름으로 로그 객체를 만들어 바인딩한다.

        `schedule run start`(시작할 때 아는 값 start)를 남기고, 끝나면 `schedule run completed`(결과·걸린 시간)를
        남긴다. 할 일의 예외(Exception)는 밖으로 내보내지 않고 `schedule run failed`(error, 메시지 없는 스택)로 남긴다.
        """
        return _ScheduleFlow(caller_origin(), name, start)

    @classmethod
    def polling(cls, name: str) -> "LogFlow":
        """짧은 주기 폴링 1회. schedule과 같지만 시작 줄과 span을 그 회차의 첫 줄 직전에 만든다.

        줄이 하나도 없는 회차는 시작·종료 줄이 모두 없다. 예외로 끝난 회차는 줄이 없었어도 시작·실패 줄을 남긴다.
        """
        return _PollingFlow(caller_origin(), name)

    @classmethod
    def kafka_record(
        cls,
        topic: str,
        partition: int,
        offset: int,
        headers: Iterable[tuple[str, bytes | str | None]],
        known_types: Iterable[str],
        event: str,
    ) -> "LogFlow":
        """Kafka 레코드 하나. 헤더의 traceparent를 부모로 consumer span을 열고 로그 객체를 바인딩한다.

        그 레코드를 처리하는 모든 줄에 topic·partition·offset과 이벤트의 ID·종류·대상(ce_id·ce_type·ce_subject)이
        붙는다. ce_type은 known_types 안이면 그대로, 밖이면 other이고, ce_id·ce_subject는 표준 표기 UUID일 때만 남긴다.
        시작 줄 `<event> event received`, 종료 줄 `<event> event processed`(결과·걸린 시간)다. 할 일이 예외로 끝나면
        종료 줄을 warn·FAILED·error.type으로 남기고 예외를 그대로 다시 던진다(원인 줄은 받는 쪽이 남긴다).

        Args:
            headers: 레코드 헤더 (이름, 값) 쌍. 같은 이름이 여럿이면 마지막 값이다.
            known_types: 이 consumer가 처리하는 ce_type.
            event: 문구에 쓰는 이벤트 이름(예: "graph refresh requested").
        """
        return _KafkaRecordFlow(
            caller_origin(), topic, partition, offset, headers, known_types, event
        )

    @classmethod
    def call(cls, target: str, url: str | None = None) -> "LogFlow":
        """외부 호출 하나. 새 로그 객체를 만들지 않고 지금 흐름의 줄로 결과 줄 하나를 남긴다.

        성공은 `<target> call completed`(걸린 시간, 응답 상태), 상대의 5xx는 같은 문구의 warn과 UPSTREAM_SERVER_ERROR,
        fail이나 예외는 `<target> call failed`(warn, error.type, 예외면 메시지 없는 스택). 예외는 그대로 다시 던진다.
        상대가 답한 실패(botocore ClientError, OpenAI APIStatusError)는 응답 상태와 상대의 오류 코드도 붙인다.
        시작은 이 호출을 부른 client 함수의 진입 줄이다.

        Args:
            target: 호출 대상(content, openai, s3, kms, kafka). 문구에 쓴다.
            url: HTTP 호출의 대상 주소. 주면 url.domain·url.full(쿼리 제외)을 붙인다.
        """
        return _CallFlow(caller_origin(), target, url)

    @classmethod
    def task(cls, name: str, *start: LogField | None) -> "LogFlow":
        """흐름 안·밖의 별도 작업. 새 로그 객체를 만들지 않고 지금 흐름의 줄로 남긴다.

        `<name> start`(시작할 때 아는 값)를 남기고 끝나면 `<name> completed`(결과 값·걸린 시간)를 남긴다. fail로 알린
        실패는 `<name> failed`(warn), 예외는 `<name> failed`(error, 메시지 없는 스택)이고 예외는 다시 던진다.
        """
        return _TaskFlow(caller_origin(), name, start)

    # ------------------------------------------------------------ 진행 중에 알리는 값

    def add(self, *fields: LogField | None) -> "LogFlow":
        """진행 중에 알게 된 대상 ID(project_id 등)를 이 흐름의 이후 모든 줄과 종료 줄에 붙인다."""
        self._bindings.append(bind(AppLogger.current()._derive(*fields)))
        return self

    def result(self, *fields: LogField | None) -> "LogFlow":
        """종료 줄에 붙일 결과 값(건수 등). 여러 번 부르면 모두 붙는다."""
        self._result.extend(field for field in fields if field is not None)
        return self

    def outcome(self, outcome: Outcome) -> "LogFlow":
        """Kafka 레코드의 결과(건너뜀, 실패 등). 알리지 않으면 PROCESSED다."""
        self._outcome = outcome
        return self

    def status(self, status: int) -> "LogFlow":
        """외부 호출의 응답 상태. 5xx면 결과 줄이 warn이 된다."""
        self._status = status
        return self

    def fail(self, error_type: str) -> "LogFlow":
        """예외 없이 끝난 실패를 알린다. 종료 줄이 warn이 되고 이 값이 error.type이다."""
        self._failure_type = error_type
        return self

    # ------------------------------------------------------------ 내부

    def __enter__(self) -> "LogFlow":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def _end(
        self,
        level: int,
        message: str,
        failure: BaseException | None = None,
        stack: bool = False,
    ) -> None:
        """종료 줄 하나. 결과 값, Kafka 결과, 응답 상태, 실패 이유, 걸린 시간을 붙인다.

        지금 바인딩된 객체로 찍으므로 흐름 문맥과 add로 더한 ID가 함께 붙는다.

        Args:
            stack: 예외의 메시지 없는 스택을 붙일지. Kafka 레코드는 받는 쪽이 원인 줄을 남기므로 붙이지 않는다.
        """
        fields: list[LogField | None] = list(self._result)
        if self._outcome is not None:
            fields.append(LogField.result("outcome", self._outcome))
        if self._status is not None:
            fields.append(LogField.status_code(self._status))
        error_type = type(failure).__name__ if failure is not None else None
        fields.append(LogField.error_type(error_type or self._failure_type))
        fields.append(LogField.error_code(self._error_code))
        if stack:
            fields.append(LogField.safe_stack_trace(failure))
        fields.extend(self._extra_end_fields())
        elapsed = (time.perf_counter_ns() - self._started) / 1_000_000_000
        fields.append(duration(elapsed))
        AppLogger.current()._emit(level, message, fields, self._origin)

    def _extra_end_fields(self) -> list[LogField | None]:
        """흐름 종류가 종료 줄에 더하는 필드(호출의 url.* 등)."""
        return []

    def _close_bindings(self) -> None:
        """add가 연 바인딩을 연 반대 순서로 닫는다."""
        while self._bindings:
            reset(self._bindings.pop())


class _ScheduleFlow(LogFlow):
    """스케줄 작업 1회. 회차 span을 열어 현재 span으로 두고, 그 span으로 로그 객체를 만든다."""

    def __init__(self, origin: Origin, name: str, start: tuple[LogField | None, ...]):
        super().__init__(origin)
        self._name = name
        self._start = start

    def __enter__(self) -> LogFlow:
        self._span = start_root_span(self._name)
        self._span_token = attach(self._span)
        logger = new_logger(*span_ids(self._span))._derive(
            LogField.schedule_name(self._name)
        )
        self._token = bind(logger)
        logger._emit(logging.INFO, SCHEDULE_START, self._start, self._origin)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                mark_failed(self._span)
                self._end(logging.ERROR, SCHEDULE_FAILED, exc, True)
            elif self._failure_type is not None:
                self._end(logging.WARNING, SCHEDULE_FAILED)
            else:
                self._end(logging.INFO, SCHEDULE_COMPLETED)
        finally:
            self._close_bindings()
            reset(self._token)
            detach(self._span_token)
            self._span.end()
        # Exception은 삼킨다(다음 회차에 다시 한다). 종료 요청 같은 BaseException은 그대로 내보낸다
        return isinstance(exc, Exception)


class _PollingFlow(LogFlow):
    """폴링 1회. 첫 줄 직전에 시작 줄과 회차 span을 만들고, 줄이 없으면 아무것도 남기지 않는다.

    회차 span은 현재 span으로 두지 않는다. 첫 줄이 어느 깊이에서 찍힐지 모르기 때문이다. 대신 그 ID를 회차 로그
    객체의 공유 칸에 넣어 회차의 모든 줄이 같은 trace를 갖게 한다.
    """

    def __init__(self, origin: Origin, name: str):
        super().__init__(origin)
        self._name = name
        self._span = None

    def __enter__(self) -> LogFlow:
        logger = new_logger(None, None)._derive(LogField.schedule_name(self._name))
        shared = logger._shared_state()

        def begin() -> None:
            # 이 회차의 첫 줄 직전: 회차 span을 열어 trace를 정하고 시작 줄을 남긴다
            self._span = start_root_span(self._name)
            shared.trace_id, shared.span_id = span_ids(self._span)
            logger._emit(logging.INFO, SCHEDULE_START, (), self._origin)

        shared.before_first_line(begin)
        self._token = bind(logger)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                # 줄이 없던 회차여도 실패 줄이 첫 줄이라 시작 줄이 먼저 남는다
                self._end(logging.ERROR, SCHEDULE_FAILED, exc, True)
            elif self._failure_type is not None:
                self._end(logging.WARNING, SCHEDULE_FAILED)
            elif self._span is not None:
                self._end(logging.INFO, SCHEDULE_COMPLETED)
        finally:
            self._close_bindings()
            reset(self._token)
            if self._span is not None:
                if exc is not None or self._failure_type is not None:
                    mark_failed(self._span)
                self._span.end()
        return isinstance(exc, Exception)


class _KafkaRecordFlow(LogFlow):
    """Kafka 레코드 하나. 헤더의 trace를 이은 consumer span을 현재 span으로 두고 그 span으로 로그 객체를 만든다."""

    def __init__(
        self,
        origin: Origin,
        topic: str,
        partition: int,
        offset: int,
        headers: Iterable[tuple[str, bytes | str | None]],
        known_types: Iterable[str],
        event: str,
    ):
        super().__init__(origin)
        self._topic, self._partition, self._offset = topic, partition, offset
        # 같은 이름의 헤더가 여럿이면 마지막 값이다(Java lastHeader와 같다)
        self._headers = {name: _text(value) for name, value in headers}
        self._known = frozenset(known_types)
        self._event = event

    def __enter__(self) -> LogFlow:
        self._span = start_consumer_span(
            f"{self._topic} process",
            self._headers.get("traceparent"),
            {
                "messaging.system": "kafka",
                "messaging.operation.type": "process",
                "messaging.destination.name": self._topic,
                "messaging.destination.partition.id": str(self._partition),
                "messaging.kafka.offset": self._offset,
            },
        )
        self._span_token = attach(self._span)
        logger = new_logger(*span_ids(self._span))._derive(
            LogField.kafka_topic(self._topic),
            LogField.kafka_partition(self._partition),
            LogField.kafka_offset(self._offset),
            LogField.cloudevent_id(self._headers.get("ce_id")),
            LogField.cloudevent_type(self._headers.get("ce_type"), self._known),
            LogField.cloudevent_subject(self._headers.get("ce_subject")),
        )
        self._token = bind(logger)
        logger._emit(logging.INFO, f"{self._event} event received", (), self._origin)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        processed = f"{self._event} event processed"
        try:
            if exc is not None:
                self._outcome = Outcome.FAILED
                mark_failed(self._span)
                self._end(logging.WARNING, processed, exc, False)
            else:
                if self._outcome is None:
                    self._outcome = Outcome.PROCESSED
                if self._failure_type is not None or self._outcome is Outcome.FAILED:
                    mark_failed(self._span)
                level = logging.INFO if self._failure_type is None else logging.WARNING
                self._end(level, processed)
        finally:
            self._close_bindings()
            reset(self._token)
            detach(self._span_token)
            self._span.end()
        return False


class _CallFlow(LogFlow):
    """외부 호출 하나. 바인딩을 바꾸지 않고 지금 흐름의 줄로 결과 줄을 남긴다."""

    def __init__(self, origin: Origin, target: str, url: str | None):
        super().__init__(origin)
        self._target = target
        self._url = url

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                # 상대가 답한 실패면 응답 상태와 상대의 오류 코드를 남긴다. 연결 실패·시간 초과는 답이 없어 남기지 않는다
                self._status, self._error_code = _answered(exc, self._status)
                self._end(logging.WARNING, f"{self._target} call failed", exc, True)
            elif self._failure_type is not None:
                self._end(logging.WARNING, f"{self._target} call failed")
            elif self._status is not None and self._status >= 500:
                self._failure_type = UPSTREAM_SERVER_ERROR
                self._end(logging.WARNING, f"{self._target} call completed")
            else:
                self._end(logging.INFO, f"{self._target} call completed")
        finally:
            self._close_bindings()
        return False

    def _extra_end_fields(self) -> list[LogField | None]:
        if self._url is None:
            return []
        try:
            host = urlsplit(self._url).hostname
        except ValueError:
            host = None
        return [LogField.url_domain(host), LogField.url_full(self._url)]


class _TaskFlow(LogFlow):
    """흐름 안·밖의 별도 작업. 바인딩을 바꾸지 않고 지금 흐름의 줄로 시작·종료 줄을 남긴다."""

    def __init__(self, origin: Origin, name: str, start: tuple[LogField | None, ...]):
        super().__init__(origin)
        self._name = name
        self._start = start

    def __enter__(self) -> LogFlow:
        AppLogger.current()._emit(
            logging.INFO, f"{self._name} start", self._start, self._origin
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                self._end(logging.ERROR, f"{self._name} failed", exc, True)
            elif self._failure_type is not None:
                self._end(logging.WARNING, f"{self._name} failed")
            else:
                self._end(logging.INFO, f"{self._name} completed")
        finally:
            self._close_bindings()
        return False


class LogScope:
    """시작·종료 줄 없이 로그 객체만 바인딩하는 범위. `with`로 열고 닫으면 이전 바인딩으로 돌아간다.

    - stored: 저장했다 나중에 처리하는 일. 저장한 traceparent의 trace로 남긴다
    - outbox_event: outbox 행 하나의 줄(발행, 발행 완료 기록). 그 행을 만든 흐름의 trace로 남긴다
    """

    def __init__(self, logger: AppLogger):
        self._logger = logger

    def __enter__(self) -> "LogScope":
        self._token = bind(self._logger)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        reset(self._token)
        return False

    @classmethod
    def stored(cls, traceparent: str | None, *context: LogField | None) -> "LogScope":
        """저장해 둔 traceparent의 trace와 문맥 필드로 바인딩한다.

        span.id는 그 값을 저장한 쪽의 span이다. 값이 없거나 형식이 틀리면 지금 흐름(회차)의 trace를 쓴다.
        """
        return cls(_stored_logger(traceparent)._derive(*context))

    @classmethod
    def outbox_event(
        cls,
        traceparent: str | None,
        schedule: str,
        event_id: UUID | str | None,
        event_type: str,
        subject: UUID | str | None,
        *ids: LogField | None,
    ) -> "LogScope":
        """outbox 행 하나의 범위. 행의 traceparent, relay 작업 이름, 이벤트의 ID·종류·대상과 도메인 ID를 붙인다.

        받는 서비스의 Kafka 레코드 줄과 같은 cloudevent 필드라 이벤트 ID 하나로 보낸 줄과 받은 줄을 함께 찾는다.

        Args:
            traceparent: 행에 저장한 traceparent. 없으면(추적 전에 쌓인 행) relay 회차의 trace가 붙는다.
            schedule: relay 작업 이름.
            ids: 이벤트가 가리키는 도메인 ID(event key인 project_id 등).
        """
        logger = _stored_logger(traceparent)._derive(
            LogField.schedule_name(schedule),
            LogField.cloudevent_id(event_id),
            LogField.cloudevent_type(event_type),
            LogField.cloudevent_subject(subject),
            *ids,
        )
        return cls(logger)


def _stored_logger(traceparent: str | None) -> AppLogger:
    """저장한 traceparent로 만든 객체. 값이 없으면 지금 바인딩된 흐름의 trace를 이어 쓴다."""
    parsed = parse_traceparent(traceparent)
    if parsed is not None:
        return new_logger(*parsed)
    return new_logger(*AppLogger.current()._effective_trace())


def _text(value: bytes | str | None) -> str | None:
    """헤더 값을 문자열로. UTF-8이 아니면 대체 문자로 바꾼다(이 값은 UUID·허용 목록 검사 뒤에만 남는다)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return bytes(value).decode("utf-8", errors="replace")


def _answered(
    failure: BaseException, status: int | None
) -> tuple[int | None, str | None]:
    """상대가 답한 실패의 (응답 상태, 상대의 오류 코드).

    - botocore ClientError: response["ResponseMetadata"]["HTTPStatusCode"], response["Error"]["Code"]
    - OpenAI APIStatusError: status_code, code
    - httpx HTTPStatusError: response.status_code
    식별자 모양이 아닌 코드는 남기지 않는다.
    """
    code = None
    response = getattr(failure, "response", None)
    if isinstance(response, dict):
        answered = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(answered, int) and answered > 0:
            status = answered
        code = response.get("Error", {}).get("Code")
    else:
        answered = getattr(failure, "status_code", None)
        if answered is None and response is not None:
            answered = getattr(response, "status_code", None)
        if isinstance(answered, int) and not isinstance(answered, bool):
            status = answered
            code = getattr(failure, "code", None)
    if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
        code = None
    return status, code
