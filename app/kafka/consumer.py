"""wire 검증·명시적 업무 처리기·재시도·DLQ를 기존 polling worker에 연결한다."""

from collections.abc import Callable, Mapping
from threading import Event
from typing import Protocol

from confluent_kafka import Consumer

from app.kafka.errors import (
    InvalidEvent,
    NotReady,
    ProcessingStopped,
    RetryableProcessingError,
)
from app.kafka.messages import (
    EVENT_TYPES,
    PROJECT_CHANGED,
    REFRESH_REQUESTED,
    IncomingEvent,
    parse_event,
)
from app.kafka.polling import PollingWorker, Record
from app.logs import AppLogger, LogField, LogFlow, Outcome, skip_entry_log

# 레코드 흐름의 시작·종료 줄 문구에 쓰는 이벤트 이름(`<event> event received`)
EVENT_NAMES = {
    PROJECT_CHANGED: "project changed",
    REFRESH_REQUESTED: "graph refresh requested",
}
# topic별 consumer group. business_runtime이 consumer 설정에 쓰고, 레코드 흐름의 consumer span(messaging.consumer.group.name)에도
# 같은 값이 남는다
GROUPS = {
    PROJECT_CHANGED: "graph-rag.project-changed",
    REFRESH_REQUESTED: "graph-rag.graph-refresh-requested",
}
# 이벤트 종류별 대상(ce_subject)의 도메인 ID 필드. 레코드 흐름의 이후 모든 줄에 project_id와 함께 붙인다
SUBJECT_FIELDS = {
    "FileChanged": LogField.file_id,
    "GraphRefreshRequested": LogField.refresh_request_id,
}


class DeadLetterPublisher(Protocol):
    def dead_letter(self, record: Record, failure: Exception) -> None:
        """DLQ 전달 확인 후 반환. 실패하면 예외를 전파한다."""
        ...


class BusinessConsumerHandler:
    """payload 업무 규칙은 필수 주입한다. no-op 기본 처리기를 제공하지 않는다.

    저장·완료 발행·Inbox가 성공한 뒤에만 업무 처리기가 반환해야 한다.
    계약 오류만 즉시 DLQ, 명시한 개별 일시 장애만 제한 재시도한다.
    전달·인증·프로그래밍 오류는 그대로 전파해 offset을 유지한다.
    """

    def __init__(
        self,
        topic: str,
        handlers: Mapping[str, Callable[[IncomingEvent], None]],
        dead_letters: DeadLetterPublisher,
        stop: Event,
        *,
        retry_delays: tuple[float, ...] = (1, 2, 4, 8, 10),
    ):
        if topic not in EVENT_TYPES:
            raise ValueError("Unsupported business topic")
        if set(handlers) != EVENT_TYPES[topic] or not all(
            callable(handler) for handler in handlers.values()
        ):
            raise ValueError("Every business event requires an explicit handler")
        if any(not 0 <= delay < float("inf") for delay in retry_delays):
            raise ValueError("Retry delays must be nonnegative and finite")
        self._topic = topic
        self._handlers = dict(handlers)
        self._dead_letters = dead_letters
        self._stop = stop
        self._delays = retry_delays

    def __call__(self, record: Record) -> None:
        """레코드 하나를 처리 스레드에서 처리한다.

        처리 스레드는 contextvars를 옮기지 않으므로 레코드마다 여기서 레코드 흐름(LogFlow.kafka_record)을 연다.
        헤더의 traceparent를 이은 consumer span과 레코드 문맥(topic·partition·offset, ce_id·ce_type·ce_subject)이
        재시도·DLQ 줄과 업무 처리기·어댑터의 모든 줄에 붙는다.
        """
        if record.topic != self._topic:
            raise ValueError("Record is outside the configured subscription")
        if self._stop.is_set():
            raise ProcessingStopped("Consumer is stopping")
        with LogFlow.kafka_record(
            record.topic,
            record.partition,
            record.offset,
            record.headers,
            EVENT_TYPES[self._topic],
            EVENT_NAMES[self._topic],
            GROUPS[self._topic],
            # 레코드의 시각. 처리를 시작하기까지 기다린 시간을 지표로 남긴다
            timestamp=record.timestamp,
        ) as flow:
            # wire 검증 → 업무 처리기 → 재시도·DLQ. 결과는 레코드 흐름의 종료 줄(outcome)에 남는다
            try:
                event = parse_event(record)
            except InvalidEvent as failure:
                self._reject(record, failure, flow)
                return
            if event is None:
                # Never log the untrusted ce_type or payload.
                # 받은 종류는 레코드 문맥의 loresentry.cloudevent.type(처리하지 않는 종류라 other)에 있다
                AppLogger.current().warn(
                    "kafka event skipped", LogField.error_type("UNKNOWN_EVENT_TYPE")
                )
                flow.outcome(Outcome.SKIPPED)
                return
            subject = SUBJECT_FIELDS.get(event.event_type)
            flow.add(
                LogField.project_id(event.project_id),
                None if subject is None else subject(event.subject),
            )
            for attempt in range(len(self._delays) + 1):
                if self._stop.is_set():
                    raise ProcessingStopped("Consumer is stopping")
                try:
                    # 업무 처리기가 이미 처리한 이벤트(inbox 중복)라고 알리면 결과를 DUPLICATE로 남긴다
                    if self._handlers[event.event_type](event) == "DUPLICATE":
                        flow.outcome(Outcome.DUPLICATE)
                    return
                except InvalidEvent as failure:
                    self._reject(record, failure, flow, event)
                    return
                except RetryableProcessingError as failure:
                    AppLogger.current().warn(
                        "kafka processing failed",
                        LogField.param("attempt", attempt + 1),
                        LogField.error_type(type(failure).__name__),
                        LogField.safe_stack_trace(failure),
                    )
                    if attempt == len(self._delays):
                        self._exhausted(record, event, failure, flow)
                        return
                    if self._stop.wait(self._delays[attempt]):
                        raise ProcessingStopped("Consumer retry stopped") from None

    def _exhausted(self, record, event, failure, flow):
        """재시도를 다 쓴 실패를 처리한다. 투영 대기는 처리기가 업무 실패로 끝낼 수 있다.

        처리기에 exhausted가 있고 실패가 NotReady면 그것을 부르고 offset을 진행한다.
        그 처리마저 거절되거나 일시 실패하면 DLQ로 보낸다.
        """
        hook = getattr(self._handlers[event.event_type], "exhausted", None)
        if hook is None or not isinstance(failure, NotReady):
            self._reject(record, failure, flow, event)
            return
        try:
            hook(event, failure)
        except (InvalidEvent, RetryableProcessingError) as hook_failure:
            self._reject(record, hook_failure, flow, event)

    def _reject(
        self,
        record: Record,
        failure: Exception,
        flow: LogFlow,
        event: IncomingEvent | None = None,
    ) -> None:
        """레코드를 DLQ로 보낸다. 해석한 이벤트면 먼저 업무 처리기의 rejected를 부른다.

        rejected는 DLQ로 가는 요청을 업무 실패로 끝낼 수 있다(최신화 요청의 FAILED 완료). 해석하지 못한 레코드는
        업무 처리기가 무엇인지 모르므로 부르지 않는다.
        """
        if self._stop.is_set():
            raise ProcessingStopped("Consumer is stopping")
        hook = (
            None
            if event is None
            else getattr(self._handlers[event.event_type], "rejected", None)
        )
        if hook is not None:
            hook(event, failure)
        self._dead_letters.dead_letter(record, failure)
        reason = type(failure).__name__
        AppLogger.current().error(
            "kafka event dead-lettered",
            LogField.error_type(reason),
            LogField.safe_stack_trace(failure),
        )
        flow.outcome(Outcome.FAILED).fail(reason)


@skip_entry_log("builds the worker once per consumer thread")
def business_worker(
    consumer: Consumer,
    topic: str,
    handlers: Mapping[str, Callable[[IncomingEvent], None]],
    dead_letters: DeadLetterPublisher,
    stop: Event,
    *,
    retry_delays: tuple[float, ...] = (1, 2, 4, 8, 10),
) -> PollingWorker:
    """polling 스레드에서 생성·run한다. consumer 설정은 consumer_config를 사용한다.

    두 업무 토픽은 별도 consumer/worker로 조립한다. 이 함수는 시작·환경변수 조회를 하지 않는다.
    PollingWorker가 성공 반환 뒤 현재 할당 세대의 offset+1만 commit한다.
    """
    handler = BusinessConsumerHandler(
        topic, handlers, dead_letters, stop, retry_delays=retry_delays
    )
    return PollingWorker(consumer, (topic,), handler)
