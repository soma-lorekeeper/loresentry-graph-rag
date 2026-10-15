"""Fake와 향후 Kafka consumer가 공유하는 처리 진입점."""

from typing import Protocol

from app.projection.models import (
    ApplyResult,
    ProjectionChange,
    ProjectionRejected,
    ProjectionUnavailable,
    canonical_uuid,
)


class ProjectionStore(Protocol):
    def apply(self, change: ProjectionChange) -> ApplyResult:
        """그래프 변경과 Inbox를 원자적으로 저장하고 확인한 결과만 반환한다."""
        ...


class GraphProjectionHandler:
    def __init__(self, store: ProjectionStore):
        self._store = store

    def handle(self, change: ProjectionChange, *, key: str) -> ApplyResult:
        try:
            canonical_uuid(key)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProjectionRejected("Invalid project key") from exc
        if key != change.project_id:
            raise ProjectionRejected("Project key does not match the change")
        result = self._store.apply(change)
        if not isinstance(result, ApplyResult):
            raise ProjectionUnavailable("Store did not confirm an apply result")
        return result
