"""레코드별 Kafka 전달 확인을 기다리는 동기식 IO 어댑터."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic

from confluent_kafka import Producer

from app.kafka.errors import DeliveryError
from app.kafka.messages import OutgoingRecord, completion_record, dead_letter_record
from app.kafka.polling import Record
from app.logs import AppLogger, LogField, LogFlow, kafka_client_logger, skip_entry_log
from app.refresh.models import Completion


@skip_entry_log("builds the producer configuration at startup")
def producer_config(bootstrap_servers: str) -> dict:
    if not bootstrap_servers.strip():
        raise ValueError("bootstrap_servers is required")
    return {
        "bootstrap.servers": bootstrap_servers,
        "acks": "all",
        "enable.idempotence": True,
        "compression.type": "zstd",
        "message.max.bytes": 6291456,
        "request.timeout.ms": 10000,
        "message.timeout.ms": 20000,
        # librdkafka 로그를 stderr 평문 대신 이 서비스의 로그 형식(log.logger: librdkafka)으로 받는다
        "logger": kafka_client_logger(),
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

    @skip_entry_log("publish and dead_letter log the entry line before sending")
    def send(self, record: OutgoingRecord) -> None:
        """레코드 하나를 보내고 전달 확인을 기다린다. 결과 줄은 `kafka call completed`(topic·partition·offset)다.

        실패는 `kafka call failed`(warn, error.type DeliveryError)이고 예외를 그대로 전파한다.
        """
        deadline = self._clock() + self._timeout
        # 전달 콜백이 받은 (error, message). 이 레코드의 확인만 담는다
        result = []

        def delivered(error, message):
            result.append((error, message))

        with LogFlow.call("kafka") as call:
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
            error, message = result[0]
            if error is not None:
                raise DeliveryError("Kafka delivery failed")
            # 받는 쪽 broker가 정한 위치. 보낸 레코드를 이 값으로 찾는다
            call.result(
                LogField.kafka_topic(record.topic),
                LogField.kafka_partition(message.partition()),
                LogField.kafka_offset(message.offset()),
            )

    def _poll(self, timeout: float) -> None:
        try:
            self._producer.poll(timeout)
        except Exception:
            raise DeliveryError("Kafka delivery polling failed") from None

    def publish(
        self,
        completion: Completion,
        *,
        occurred_at: datetime | None = None,
        traceparent: str | None = None,
    ) -> None:
        """최신화 완료 이벤트를 보낸다.

        Args:
            traceparent: 완료를 기록한 요청 처리의 traceparent(InboxEvent에 저장한 값). 있으면 헤더로 실어
                받는 서비스(content)가 그 trace를 잇는다. 없으면 헤더를 싣지 않는다.
        """
        AppLogger.current().info(
            "graph refresh completion publish",
            LogField.param("refresh_outcome", completion.outcome),
        )
        self.send(
            completion_record(
                completion,
                now=occurred_at or datetime.now(UTC),
                traceparent=traceparent,
            )
        )

    def dead_letter(self, record: Record, failure: Exception) -> None:
        """원본 레코드를 그 topic의 DLQ로 보낸다. 원본 헤더(traceparent 포함)를 그대로 싣는다."""
        AppLogger.current().info("kafka dead letter publish")
        self.send(dead_letter_record(record, failure))
