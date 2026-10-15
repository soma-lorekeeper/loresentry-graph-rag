"""하나의 polling 소유자와 하나의 작업 스레드를 쓰는 초기 Kafka runtime."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition


@dataclass(frozen=True)
class Record:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None
    headers: tuple[tuple[str, bytes | None], ...] = ()


def consumer_config(bootstrap_servers: str, group_id: str) -> dict:
    if not bootstrap_servers.strip() or not group_id.strip():
        raise ValueError("bootstrap_servers and group_id are required")
    return {
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "auto.offset.reset": "earliest",
        "allow.auto.create.topics": False,
        # The callbacks below use full eager assignments, not cooperative deltas.
        "group.protocol": "classic",
        "partition.assignment.strategy": "range",
    }


class PollingWorker:
    """처리 중 전체 할당을 pause하고 poll을 유지한다. 인스턴스는 한 번만 실행한다.

    handler의 성공 반환은 저장·발행 등 필요한 효과가 확인되었다는 계약이다.
    실패하면 commit 없이 종료한다. 재시도·Inbox는 handler와 재전달의 책임이다.
    Consumer 메서드는 모두 run을 호출한 스레드에서 실행한다.
    """

    def __init__(
        self,
        consumer: Consumer,
        topics: tuple[str, ...],
        handler: Callable[[Record], None],
        *,
        poll_timeout: float = 0.1,
    ):
        if not topics or any(not topic.strip() for topic in topics):
            raise ValueError("At least one nonempty topic is required")
        if not 0 < poll_timeout <= 1:
            raise ValueError("poll_timeout must be in (0, 1]")
        self._consumer = consumer
        self._topics = topics
        self._handler = handler
        self._timeout = poll_timeout
        self._epoch = 0
        self._assigned: list[TopicPartition] = []
        self._future: Future | None = None
        self._started = False
        self._stopping = False

    def _assign(self, consumer, partitions):
        self._epoch += 1
        self._assigned = list(partitions)
        consumer.assign(partitions)
        if self._future is not None or self._stopping:
            consumer.pause(partitions)

    def _revoke(self, consumer, partitions):
        self._epoch += 1
        self._assigned = []
        consumer.unassign()

    def run(self, stop: Event) -> None:
        if self._started:
            raise RuntimeError("PollingWorker can only run once")
        self._started = True
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kafka-handler")
        record = None
        epoch = None
        try:
            self._consumer.subscribe(
                list(self._topics),
                on_assign=self._assign,
                on_revoke=self._revoke,
                on_lost=self._revoke,
            )
            while True:
                self._stopping = stop.is_set()
                if self._stopping and self._future is None:
                    break
                if self._future is not None and self._future.done():
                    # Errors must never allow this or later offsets to commit.
                    self._future.result()
                    self._future = None
                    if epoch == self._epoch:
                        offsets = self._consumer.commit(
                            offsets=[
                                TopicPartition(
                                    record.topic, record.partition, record.offset + 1
                                )
                            ],
                            asynchronous=False,
                        )
                        for offset in offsets:
                            if offset.error is not None:
                                raise KafkaException(offset.error)
                    if self._stopping:
                        break
                    if self._assigned:
                        self._consumer.resume(self._assigned)
                message = self._consumer.poll(self._timeout)
                if message is None:
                    continue
                error = message.error()
                if error is not None:
                    if error.code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(error)
                if self._future is not None:
                    raise RuntimeError("Received a record while processing was paused")
                # A signal may have arrived during poll. Leave this record uncommitted.
                if stop.is_set():
                    break
                record = Record(
                    topic=message.topic(),
                    partition=message.partition(),
                    offset=message.offset(),
                    key=message.key(),
                    value=message.value(),
                    headers=tuple(message.headers() or ()),
                )
                self._consumer.pause(self._assigned)
                epoch = self._epoch
                self._future = executor.submit(self._handler, record)
        finally:
            try:
                # auto commit is disabled, including on close.
                self._consumer.close()
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
