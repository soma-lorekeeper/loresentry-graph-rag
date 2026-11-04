"""Fake와 향후 Kafka consumer가 공유하는 처리 진입점."""

from typing import Protocol

from app.logs import AppLogger, LogField
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
        # 문서 제목·설명·관계 설명은 남기지 않는다. 건수와 순서 번호만 남긴다
        AppLogger.current().info(
            "graph projection apply",
            LogField.project_id(change.project_id),
            LogField.param("sequence", change.sequence),
            LogField.param("files", change.files),
            LogField.param("relations", change.relations),
            LogField.param("deleted_file_ids", change.deleted_file_ids),
        )
        try:
            canonical_uuid(key)
        except (ValueError, TypeError, AttributeError) as exc:
            AppLogger.current().warn(
                "graph projection rejected", LogField.error_type("INVALID_PROJECT_KEY")
            )
            raise ProjectionRejected("Invalid project key") from exc
        if key != change.project_id:
            AppLogger.current().warn(
                "graph projection rejected", LogField.error_type("PROJECT_KEY_MISMATCH")
            )
            raise ProjectionRejected("Project key does not match the change")
        result = self._store.apply(change)
        if not isinstance(result, ApplyResult):
            raise ProjectionUnavailable("Store did not confirm an apply result")
        # APPLIED: 새로 반영함, DUPLICATE: 이미 반영한 이벤트(Inbox)
        AppLogger.current().info(
            "graph projection applied", LogField.result("inbox", result)
        )
        return result
