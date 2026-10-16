"""레코드별 Kafka 전달 확인을 기다리는 동기식 IO 어댑터."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic

from confluent_kafka import Producer

from app.kafka.errors import DeliveryError
from app.kafka.messages import OutgoingRecord, completion_record, dead_letter_record
from app.kafka.polling import Record
from app.refresh.models import Completion


def producer_config(bootstrap_servers: str) -> dict:
    if not bootstrap_servers.strip():
        raise ValueError("bootstrap_servers is required")
    return {
        "bootstrap.servers": bootstrap_servers,
        "acks": "all",
        "enable.idempotence": True,
        "compression.type": "zstd",
        "message.max.bytes": 4194304,
        "request.timeout.ms": 10000,
        "message.timeout.ms": 20000,
    }


class KafkaPublisher:
    """worker별로 소유한다. 성공 반환은 해당 record의 delivery callback 성공이다.

    타임아웃 뒤 늦은 성공도 가능하다. 호출자는 같은 ID로 재발행을 허용해야 한다.
    주입한 Producer는 호출 측이 소유하며 shutdown 시 flush를 담당한다.
    """

    def __init__(
        self,
        producer: Producer,
        *,
        timeout: float = 25,
        clock: Callable[[], float] = monotonic,
    ):
        if not 0 < timeout < float("inf"):
            raise ValueError("Delivery timeout must be positive and finite")
        self._producer = producer
        self._timeout = timeout
        self._clock = clock

    def send(self, record: OutgoingRecord) -> None:
        deadline = self._clock() + self._timeout
        result = []

        def delivered(error, message):
            result.append(error)

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeliveryError("Kafka enqueue deadline exceeded")
            try:
                self._producer.produce(
                    record.topic,
                    key=record.key,
                    value=record.value,
                    headers=list(record.headers),
                    on_delivery=delivered,
                )
                break
            except BufferError:
                self._poll(min(0.1, remaining))
            except Exception:
                # Kafka exceptions can contain broker details or supplied data.
                raise DeliveryError("Kafka enqueue failed") from None
        while not result:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeliveryError("Kafka delivery deadline exceeded")
            self._poll(min(0.1, remaining))
        if result[0] is not None:
            raise DeliveryError("Kafka delivery failed")

    def _poll(self, timeout: float) -> None:
        try:
            self._producer.poll(timeout)
        except Exception:
            raise DeliveryError("Kafka delivery polling failed") from None

    def publish(self, completion: Completion) -> None:
        self.send(completion_record(completion, now=datetime.now(UTC)))

    def dead_letter(self, record: Record, failure: Exception) -> None:
        self.send(dead_letter_record(record, failure))
