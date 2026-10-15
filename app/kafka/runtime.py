"""환경 설정과 서버 수명주기로 소유하는 fake Kafka polling runtime."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event, Thread
from typing import Self

from confluent_kafka import Consumer

from app.kafka.fake import FakeConsumerHandler, FakeProducer
from app.kafka.polling import PollingWorker, consumer_config

logger = logging.getLogger(__name__)


def validate_fake_subscription(topics: tuple[str, ...], group_id: str) -> None:
    if not topics or any(
        not topic.startswith("graph-rag.sandbox.") or topic == "graph-rag.sandbox."
        for topic in topics
    ):
        raise ValueError("Fake processing requires graph-rag.sandbox.* topics")
    if not group_id.startswith("graph-rag-fake-") or group_id == "graph-rag-fake-":
        raise ValueError("Fake processing requires a graph-rag-fake-* group")


@dataclass(frozen=True)
class FakePollingSettings:
    bootstrap_servers: str
    topics: tuple[str, ...] = ("graph-rag.sandbox.polling",)
    group_id: str = "graph-rag-fake-polling"

    def __post_init__(self):
        consumer_config(self.bootstrap_servers, self.group_id)
        validate_fake_subscription(self.topics, self.group_id)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self | None:
        enabled = env.get("KAFKA_POLLING_ENABLED", "false").strip().lower()
        if enabled not in {"true", "false"}:
            raise ValueError("KAFKA_POLLING_ENABLED must be true or false")
        if enabled == "false":
            return None
        return cls(
            bootstrap_servers=env.get("KAFKA_BOOTSTRAP_SERVERS", "").strip(),
            topics=tuple(
                topic.strip()
                for topic in env.get(
                    "KAFKA_POLLING_TOPICS", "graph-rag.sandbox.polling"
                ).split(",")
            ),
            group_id=env.get(
                "KAFKA_POLLING_GROUP_ID", "graph-rag-fake-polling"
            ).strip(),
        )


class FakePollingRuntime:
    """Consumer 생성·종료까지 polling 스레드에서 수행한다. 한 번만 시작한다."""

    def __init__(self, settings: FakePollingSettings):
        self._settings = settings
        self._stop = Event()
        self._ready = Event()
        self._error: BaseException | None = None
        self._thread: Thread | None = None
        self.producer = FakeProducer()

    @property
    def healthy(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._error is None
            and not self._stop.is_set()
        )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Kafka polling runtime can only start once")
        self._thread = Thread(target=self._run, name="kafka-polling", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self._stop.set()
            raise RuntimeError("Kafka polling runtime startup timed out")
        if self._error is not None:
            raise RuntimeError("Kafka polling runtime failed to start") from self._error

    def _run(self) -> None:
        try:
            config = consumer_config(
                self._settings.bootstrap_servers, self._settings.group_id
            )
            # Keep close/commit network waits inside the pod's shutdown budget.
            config["socket.timeout.ms"] = 10000
            consumer = Consumer(config)
            worker = PollingWorker(
                consumer, self._settings.topics, FakeConsumerHandler(self.producer)
            )
            self._ready.set()
            logger.info("Kafka polling started with fake consumer and producer")
            worker.run(self._stop)
            if not self._stop.is_set():
                raise RuntimeError("Kafka polling worker stopped unexpectedly")
        except BaseException as exc:
            self._error = exc
            logger.exception("Kafka polling worker failed")
        finally:
            self._ready.set()
            logger.info("Kafka polling stopped; fake_published=%s", self.producer.count)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                raise RuntimeError("Kafka polling runtime shutdown timed out")
