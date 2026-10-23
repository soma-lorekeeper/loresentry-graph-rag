"""요청이 의존하는 최신 Content 이벤트 반영과 영구 삭제 여부를 writer에서 확인한다."""

from app.adapters.neptune_client import NeptuneGremlinClient, NeptuneUnavailable
from app.adapters.projection_gremlin import literal
from app.kafka.errors import InvalidEvent, NotReady, RetryableProcessingError
from app.projection.models import CONSUMER_NAME


class ProjectionReadiness(NeptuneGremlinClient):
    def check(self, project_id: str, event_ids: tuple[str, ...]) -> None:
        ids = ",".join(
            literal(f"inbox:{CONSUMER_NAME}:{event}") for event in set(event_ids)
        )
        count = (
            f"__.V({ids}).hasLabel('InboxEvent').has('project_id',{literal(project_id)}).count()"
            if ids
            else "__.constant(0)"
        )
        query = (
            "g.inject(0).project('deleted','count')"
            f".by(__.V({literal('projection:project:' + project_id)}).has('deleted',true).count())"
            f".by({count})"
        )
        try:
            result = self.execute(query)
        except NeptuneUnavailable:
            raise RetryableProcessingError("Projection readiness unavailable") from None
        if len(result) != 1 or not isinstance(result[0], dict):
            raise RetryableProcessingError("Invalid projection readiness")
        if result[0].get("deleted") == 1:
            raise InvalidEvent("deleted:project")
        if result[0].get("count") != len(set(event_ids)):
            raise NotReady("PROJECTION_NOT_READY", "Projection has not caught up")
