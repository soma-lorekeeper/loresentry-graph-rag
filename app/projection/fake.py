"""브로커 없이 순서·실패 중단·ack 경계를 확인하는 수신 측."""

from dataclasses import dataclass

from app.projection.models import ApplyResult, ProjectionChange, ProjectionError
from app.projection.service import GraphProjectionHandler


@dataclass(frozen=True)
class FakeRecord:
    key: str
    change: ProjectionChange
    partition: int = 0
    offset: int = 0


class FakeConsumer:
    """명시적으로 resume할 때까지 실패 파티션을 멈춘다. 상태는 메모리에만 있다."""

    def __init__(
        self, handler: GraphProjectionHandler, records: tuple[FakeRecord, ...]
    ):
        previous: dict[int, int] = {}
        for record in records:
            if (
                type(record.partition) is not int
                or type(record.offset) is not int
                or record.partition < 0
                or record.offset < 0
                or record.offset <= previous.get(record.partition, -1)
            ):
                raise ValueError("Records must have increasing nonnegative offsets")
            previous[record.partition] = record.offset
        self._handler = handler
        self._records = records
        self.committed_offsets: dict[int, int] = {}
        self.failures: dict[int, ProjectionError] = {}
        self.results: list[tuple[FakeRecord, ApplyResult]] = []

    def run(self) -> None:
        for record in self._records:
            if record.partition in self.failures:
                continue
            if record.offset < self.committed_offsets.get(record.partition, 0):
                continue
            try:
                result = self._handler.handle(record.change, key=record.key)
            except ProjectionError as exc:
                self.failures[record.partition] = exc
                continue
            self.results.append((record, result))
            self.committed_offsets[record.partition] = record.offset + 1

    def resume(self, partition: int) -> None:
        self.failures.pop(partition, None)
