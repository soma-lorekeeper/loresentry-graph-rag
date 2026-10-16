"""완료 Inbox와 원자적으로 저장한 발행 대기를 Kafka로 전달한다."""

import logging
from threading import Event

from app.kafka.errors import DeliveryError
from app.refresh.errors import RefreshFailure
from app.refresh.ports import RefreshCompletionPublisher, RefreshOutbox

logger = logging.getLogger(__name__)


class RefreshOutboxRelay:
    def __init__(self, store: RefreshOutbox, publisher: RefreshCompletionPublisher):
        self.store, self.publisher = store, publisher

    def drain(self, stop: Event) -> int:
        sent = 0
        for entry in self.store.pending():
            if stop.is_set():
                break
            self.publisher.publish(entry.completion, occurred_at=entry.created_at)
            # 전달 확인 전에는 SENT로 바꾸지 않는다. ack/DB 응답 유실은 중복 발행 가능.
            self.store.mark_sent(entry)
            sent += 1
        return sent

    def run(self, stop: Event) -> None:
        delay = 1
        while not stop.is_set():
            try:
                sent = self.drain(stop)
                delay = 1
            except (DeliveryError, RefreshFailure) as failure:
                if (
                    isinstance(failure, RefreshFailure)
                    and not failure.failure.retryable
                ):
                    raise
                logger.warning(
                    "Refresh Outbox delivery deferred: reason=%s",
                    type(failure).__name__,
                )
                stop.wait(delay)
                delay = min(delay * 2, 30)
            else:
                if not sent:
                    stop.wait(1)
