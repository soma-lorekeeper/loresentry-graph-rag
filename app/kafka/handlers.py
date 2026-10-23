"""Content 업무 payload를 내부 투영·refresh 모델로 변환하고 유스케이스를 호출한다."""

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from app.kafka.errors import InvalidEvent, RetryableProcessingError
from app.kafka.messages import IncomingEvent
from app.projection.models import (
    FileState,
    Identifier,
    ProjectionChange,
    ProjectionUnavailable,
    RelationState,
)
from app.projection.service import GraphProjectionHandler
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import (
    ArtifactRef,
    ChangedDocumentRef,
    DocumentState,
    JobKey,
    ModelSettings,
    RefreshRequest,
    SelectionSettings,
)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class WireProperty(WireModel):
    key: StrictStr
    value: StrictStr


class WireFile(WireModel):
    """FileChanged의 문서 상태. episode_id·body_text는 그래프에 쓰지 않아 무시한다."""

    title: StrictStr
    folder_code: StrictStr
    state: Literal["ACTIVE", "TRASHED"]
    properties: list[WireProperty]


class WireRelation(WireModel):
    relation_id: Identifier
    target_document_id: Identifier
    description: StrictStr = ""


class FileChanged(WireModel):
    file_id: Identifier
    project_id: Identifier
    change_type: Literal["CREATED", "UPDATED", "TRASHED", "RESTORED", "DELETED"]
    file: WireFile | None = None
    relations: list[WireRelation] | None = None


class ChangedFile(WireModel):
    """S3 입력에 실린 문서. Content는 ACTIVE 문서만 보내므로 상태가 없다."""

    file_id: Identifier
    revision_no: Annotated[StrictInt, Field(ge=0)]


class InputRef(WireModel):
    bucket: Annotated[StrictStr, Field(min_length=1)]
    key: Annotated[StrictStr, Field(min_length=1)]


class RefreshRequested(WireModel):
    """Content의 GraphRefreshRequested. 모델·프롬프트는 GraphRAG가 정해 싣지 않는다."""

    project_id: Identifier
    request_id: Identifier
    user_id: Identifier
    input_fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    input: InputRef
    files: Annotated[list[ChangedFile], Field(min_length=1)]
    required_events: list[Identifier]


def projection_change(event: IncomingEvent) -> ProjectionChange:
    if event.sequence is None:
        raise InvalidEvent("missing:ce_sequence")
    base = dict(
        event_id=event.event_id,
        project_id=event.project_id,
        event_type=event.event_type,
        sequence=event.sequence,
    )
    try:
        if event.event_type == "ProjectDeleted":
            return ProjectionChange(**base)
        wire = FileChanged.model_validate(event.payload)
        base["snapshot_file_id"] = wire.file_id
        if wire.change_type == "DELETED":
            return ProjectionChange(**base, deleted_file_ids=(wire.file_id,))
        # DELETED가 아니면 change_type과 무관하게 문서의 전체 스냅샷이다(TRASHED 포함).
        if wire.file is None or wire.relations is None:
            raise InvalidEvent("missing:file-snapshot")
        # Content는 관계를 low 문서의 이벤트에만 싣는다. 대상이 low인 관계는
        # RelationState의 low < high 검증에서 거절되어 DLQ로 간다.
        relations = tuple(
            RelationState(
                id=relation.relation_id,
                low_document_id=wire.file_id,
                high_document_id=relation.target_document_id,
                description=relation.description,
            )
            for relation in wire.relations
        )
        file = FileState(
            id=wire.file_id,
            title=wire.file.title,
            folder_code=wire.file.folder_code,
            # 설명은 별도 필드가 아니라 key가 description인 첫 속성이다.
            description=next(
                (p.value for p in wire.file.properties if p.key == "description"),
                "",
            ),
            trashed=wire.file.state == "TRASHED",
        )
        return ProjectionChange(**base, files=(file,), relations=relations)
    except (ValidationError, ValueError):
        raise InvalidEvent("invalid:projection-payload") from None


def refresh_request(
    event: IncomingEvent, model_settings: ModelSettings
) -> tuple[RefreshRequest, str, tuple[str, ...]]:
    """요청 이벤트를 내부 요청으로 바꾼다. 모델 설정은 기동 때 정한 값을 쓴다."""
    try:
        wire = RefreshRequested.model_validate(event.payload)
        if len({file.file_id for file in wire.files}) != len(wire.files):
            raise ValueError("Duplicate input document")
        request = RefreshRequest(
            job=JobKey(wire.project_id, wire.request_id, wire.input_fingerprint),
            input_ref=ArtifactRef(wire.input.bucket, wire.input.key),
            # 실린 문서는 모두 ACTIVE이며 관계 탐색의 시작 문서가 된다.
            seed_ids=tuple(file.file_id for file in wire.files),
            changed_documents=tuple(
                ChangedDocumentRef(file.file_id, file.revision_no, DocumentState.ACTIVE)
                for file in wire.files
            ),
            model_settings=model_settings,
            settings=SelectionSettings(),
        )
        return request, wire.user_id, tuple(wire.required_events)
    except (ValidationError, ValueError):
        raise InvalidEvent("invalid:refresh-request") from None


class ProjectionEventHandler:
    def __init__(self, handler: GraphProjectionHandler):
        self._handler = handler

    def __call__(self, event: IncomingEvent) -> None:
        change = projection_change(event)
        try:
            self._handler.handle(change, key=event.project_id)
        except ProjectionUnavailable:
            raise RetryableProcessingError("Projection unavailable") from None


class RefreshEventHandler:
    def __init__(self, run: Callable, ready: Callable, model_settings: ModelSettings):
        self._run = run
        self._ready = ready
        self._model_settings = model_settings

    def __call__(self, event: IncomingEvent) -> None:
        request, user_id, required_events = refresh_request(event, self._model_settings)
        try:
            self._ready(request.job.project_id, required_events)
            self._run(request, user_id)
        except RequestConflict:
            raise InvalidEvent("conflict:refresh-request") from None
        except RefreshFailure as failure:
            if failure.failure.retryable:
                raise RetryableProcessingError(
                    "Refresh dependency unavailable"
                ) from None
            raise InvalidEvent("invalid:refresh-dependency") from None
