"""실제 업무·발행 대신 메시지 위치만 기록하는 sandbox 처리기."""

from collections import deque
from dataclasses import dataclass

from app.kafka.polling import Record
from app.logs import LogField, LogFlow, skip_entry_log

# sandbox 레코드를 받는 consumer 레이어다(log.layer)
LOG_LAYER = "consumer"


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

    @skip_entry_log("records the sandbox receipt in memory")
    def publish(self, receipt: FakeReceipt) -> None:
        self.published.append(receipt)
        self.count += 1


class FakeConsumerHandler:
    """payload를 해석하거나 Neptune·S3·Content·LLM을 호출하지 않는다."""

    def __init__(self, producer: FakeProducer, group: str):
        self._producer = producer
        # 레코드를 받는 consumer group. 레코드 흐름의 consumer span(messaging.consumer.group.name)에 남는다
        self._group = group

    def __call__(self, record: Record) -> None:
        """레코드 위치만 기록한다. 처리 스레드에서 레코드마다 레코드 흐름을 연다(헤더의 trace를 잇는다).

        업무 이벤트를 처리하지 않으므로 처리하는 ce_type이 없다(받은 종류는 모두 other로 남는다).
        """
        with LogFlow.kafka_record(
            record.topic,
            record.partition,
            record.offset,
            record.headers,
            (),
            "sandbox record",
            self._group,
        ) as flow:
            self._producer.publish(
                FakeReceipt(
                    record.topic,
                    record.partition,
                    record.offset,
                    len(record.value or b""),
                )
            )
            flow.result(LogField.result("value", record.value or b""))
