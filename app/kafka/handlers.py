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


class WireFile(WireModel):
    title: StrictStr
    folder_code: StrictStr
    description: StrictStr = ""


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
    file_id: Identifier
    revision_no: Annotated[StrictInt, Field(ge=0)]
    state: Literal["ACTIVE", "TRASHED", "DELETED"]


class InputRef(WireModel):
    bucket: Annotated[StrictStr, Field(min_length=1)]
    key: Annotated[StrictStr, Field(min_length=1)]


class ModelProfile(WireModel):
    model: Annotated[StrictStr, Field(min_length=1)]
    prompt_version: Annotated[StrictStr, Field(min_length=1)]
    schema_version: Annotated[StrictStr, Field(min_length=1)]


class RefreshRequested(WireModel):
    project_id: Identifier
    request_id: Identifier
    user_id: Identifier
    input_fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    input: InputRef
    files: Annotated[list[ChangedFile], Field(min_length=1, max_length=20)]
    model_settings: ModelProfile
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
        if wire.change_type == "TRASHED":
            return ProjectionChange(**base, trashed_file_ids=(wire.file_id,))
        if wire.file is None or wire.relations is None:
            raise InvalidEvent("missing:file-snapshot")
        relations = []
        for relation in wire.relations:
            low, high = sorted((wire.file_id, relation.target_document_id))
            relations.append(
                RelationState(
                    id=relation.relation_id,
                    low_document_id=low,
                    high_document_id=high,
                    description=relation.description,
                )
            )
        return ProjectionChange(
            **base,
            files=(FileState(id=wire.file_id, **wire.file.model_dump()),),
            relations=tuple(relations),
        )
    except (ValidationError, ValueError):
        raise InvalidEvent("invalid:projection-payload") from None


def refresh_request(
    event: IncomingEvent,
) -> tuple[RefreshRequest, str, tuple[str, ...]]:
    try:
        wire = RefreshRequested.model_validate(event.payload)
        if len({file.file_id for file in wire.files}) != len(wire.files):
            raise ValueError("Duplicate input document")
        from app.adapters.llm_schema import GENERATION_VERSIONS
        from app.refresh.prompt_versions import SUPPORTED_PROMPT_VERSIONS

        if (
            wire.model_settings.prompt_version not in SUPPORTED_PROMPT_VERSIONS
            or wire.model_settings.schema_version not in GENERATION_VERSIONS
        ):
            raise ValueError("Unsupported model profile")
        request = RefreshRequest(
            job=JobKey(wire.project_id, wire.request_id, wire.input_fingerprint),
            input_ref=ArtifactRef(wire.input.bucket, wire.input.key),
            seed_ids=tuple(
                file.file_id for file in wire.files if file.state == "ACTIVE"
            ),
            changed_documents=tuple(
                ChangedDocumentRef(
                    file.file_id, file.revision_no, DocumentState(file.state)
                )
                for file in wire.files
            ),
            model_settings=ModelSettings(**wire.model_settings.model_dump()),
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
    def __init__(self, run: Callable, ready: Callable):
        self._run = run
        self._ready = ready

    def __call__(self, event: IncomingEvent) -> None:
        request, user_id, required_events = refresh_request(event)
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
