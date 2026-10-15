"""실제 업무·발행 대신 메시지 위치만 기록하는 sandbox 처리기."""

from collections import deque
from dataclasses import dataclass

from app.kafka.polling import Record


@dataclass(frozen=True)
class FakeReceipt:
    topic: str
    partition: int
    offset: int
    value_bytes: int


class FakeProducer:
    """브로커에 발행하지 않는다. 최근 100개 기록과 누적 개수만 보관한다."""

    def __init__(self):
        self.published: deque[FakeReceipt] = deque(maxlen=100)
        self.count = 0

    def publish(self, receipt: FakeReceipt) -> None:
        self.published.append(receipt)
        self.count += 1


class FakeConsumerHandler:
    """payload를 해석하거나 Neptune·S3·Content·LLM을 호출하지 않는다."""

    def __init__(self, producer: FakeProducer):
        self._producer = producer

    def __call__(self, record: Record) -> None:
        self._producer.publish(
            FakeReceipt(
                record.topic, record.partition, record.offset, len(record.value or b"")
            )
        )
