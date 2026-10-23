"""wire 검증·명시적 업무 처리기·재시도·DLQ를 기존 polling worker에 연결한다."""

import logging
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
from app.kafka.messages import EVENT_TYPES, IncomingEvent, parse_event
from app.kafka.polling import PollingWorker, Record

logger = logging.getLogger(__name__)


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
        if record.topic != self._topic:
            raise ValueError("Record is outside the configured subscription")
        if self._stop.is_set():
            raise ProcessingStopped("Consumer is stopping")
        try:
            event = parse_event(record)
        except InvalidEvent as failure:
            self._reject(record, failure)
            return
        if event is None:
            # Never log the untrusted ce_type or payload.
            logger.warning(
                "Kafka event skipped: topic=%s partition=%s offset=%s",
                record.topic,
                record.partition,
                record.offset,
            )
            return
        for attempt in range(len(self._delays) + 1):
            if self._stop.is_set():
                raise ProcessingStopped("Consumer is stopping")
            try:
                self._handlers[event.event_type](event)
                return
            except InvalidEvent as failure:
                self._reject(record, failure)
                return
            except RetryableProcessingError as failure:
                logger.warning(
                    "Kafka processing failed: topic=%s partition=%s offset=%s "
                    "event=%s attempt=%s",
                    record.topic,
                    record.partition,
                    record.offset,
                    event.event_id,
                    attempt + 1,
                )
                if attempt == len(self._delays):
                    self._exhausted(record, event, failure)
                    return
                if self._stop.wait(self._delays[attempt]):
                    raise ProcessingStopped("Consumer retry stopped") from None

    def _exhausted(self, record, event, failure):
        """재시도를 다 쓴 실패를 처리한다. 투영 대기는 처리기가 업무 실패로 끝낼 수 있다.

        처리기에 exhausted가 있고 실패가 NotReady면 그것을 부르고 offset을 진행한다.
        그 처리마저 거절되거나 일시 실패하면 DLQ로 보낸다.
        """
        hook = getattr(self._handlers[event.event_type], "exhausted", None)
        if hook is None or not isinstance(failure, NotReady):
            self._reject(record, failure)
            return
        try:
            hook(event, failure)
        except (InvalidEvent, RetryableProcessingError) as hook_failure:
            self._reject(record, hook_failure)

    def _reject(self, record: Record, failure: Exception) -> None:
        if self._stop.is_set():
            raise ProcessingStopped("Consumer is stopping")
        self._dead_letters.dead_letter(record, failure)
        logger.error(
            "Kafka event dead-lettered: topic=%s partition=%s offset=%s",
            record.topic,
            record.partition,
            record.offset,
        )


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
