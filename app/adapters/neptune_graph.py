"""실제 Neptune에 현재 그래프와 Inbox를 한 요청으로 반영한다."""

from datetime import UTC, datetime

from app.adapters.neptune_client import (
    NeptuneGremlinClient,
    NeptuneQueryRejected,
    NeptuneUnavailable,
)
from app.adapters.projection_gremlin import REJECTIONS, build_projection_query
from app.projection.models import (
    ApplyResult,
    ProjectionChange,
    ProjectionRejected,
    ProjectionUnavailable,
)


class NeptuneProjectionStore(NeptuneGremlinClient):
    """공통 transport를 사용하고 쓰기·중복 확인 결과만 성공으로 반환한다."""

    def apply(self, change: ProjectionChange) -> ApplyResult:
        processed_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        query = build_projection_query(change, processed_at)
        try:
            data = self.execute(query, rejection_codes=REJECTIONS)
        except NeptuneQueryRejected as exc:
            raise ProjectionRejected(str(exc)) from exc
        except NeptuneUnavailable as exc:
            raise ProjectionUnavailable(str(exc)) from exc
        if data not in (["APPLIED"], ["DUPLICATE"]):
            raise ProjectionUnavailable("Neptune did not confirm one apply result")
        return ApplyResult(data[0])
