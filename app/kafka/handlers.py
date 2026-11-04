"""Content 업무 payload를 내부 투영·refresh 모델로 변환하고 유스케이스를 호출한다."""

import base64
from collections.abc import Callable
from contextlib import contextmanager
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from app.kafka.errors import InvalidEvent, NotReady, RetryableProcessingError
from app.kafka.messages import IncomingEvent
from app.logs import AppLogger, LogField, skip_entry_log
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
    Failure,
    JobKey,
    ModelSettings,
    RefreshRequest,
    SelectionSettings,
)

# 충돌한 최신화 요청을 거절할 때의 계약 검사 코드. 이 거절은 FAILED 완료를 남기지 않는다
CONFLICT = "conflict:refresh-request"
# DLQ로 보내는 최신화 요청을 끝내는 FAILED 완료의 고정 코드·문구. 예외 메시지는 싣지 않는다
PROCESSING_FAILED = "PROCESSING_FAILED"
PROCESSING_FAILED_MESSAGE = "Refresh request could not be processed"


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
    location: InputRef
    files: Annotated[list[ChangedFile], Field(min_length=1)]
    required_events: list[Identifier]
    wrapped_key: Annotated[StrictStr, Field(min_length=1)] | None = None


@skip_entry_log("pure message conversion")
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


@skip_entry_log("pure message conversion")
def refresh_request(
    event: IncomingEvent, model_settings: ModelSettings
) -> tuple[RefreshRequest, str, tuple[str, ...], bytes | None]:
    """요청 이벤트를 내부 요청으로 바꾼다. 모델 설정은 기동 때 정한 값을 쓴다."""
    try:
        wire = RefreshRequested.model_validate(event.payload)
        if len({file.file_id for file in wire.files}) != len(wire.files):
            raise ValueError("Duplicate input document")
        wrapped_key = (
            None
            if wire.wrapped_key is None
            else base64.b64decode(wire.wrapped_key, validate=True)
        )
        request = RefreshRequest(
            job=JobKey(wire.project_id, wire.request_id, wire.input_fingerprint),
            input_ref=ArtifactRef(wire.location.bucket, wire.location.key),
            # 실린 문서는 모두 ACTIVE이며 관계 탐색의 시작 문서가 된다.
            seed_ids=tuple(file.file_id for file in wire.files),
            changed_documents=tuple(
                ChangedDocumentRef(file.file_id, file.revision_no, DocumentState.ACTIVE)
                for file in wire.files
            ),
            model_settings=model_settings,
            settings=SelectionSettings(),
        )
        return request, wire.user_id, tuple(wire.required_events), wrapped_key
    except (ValidationError, ValueError):
        raise InvalidEvent("invalid:refresh-request") from None


class ProjectionEventHandler:
    def __init__(self, handler: GraphProjectionHandler):
        self._handler = handler

    def __call__(self, event: IncomingEvent) -> str:
        """문서 변경·프로젝트 삭제를 그래프에 반영한다. 반영 결과(APPLIED·DUPLICATE)를 소비자에게 돌려준다."""
        AppLogger.current().info("projection event handle")
        change = projection_change(event)
        try:
            return self._handler.handle(change, key=event.project_id)
        except ProjectionUnavailable:
            raise RetryableProcessingError("Projection unavailable") from None


class RefreshEventHandler:
    """최신화 요청을 실행한다. 투영이 끝내 따라오지 않으면 FAILED 완료로 요청을 끝낸다.

    Args:
        run: (request, user_id, wrapped_key)로 갱신안을 만들고 완료를 기록한다.
        ready: (project_id, required_events)로 투영 준비를 확인한다.
        model_settings: 기동 때 정한 모델·프롬프트·스키마 버전.
        fail: (request, user_id, wrapped_key, failure)로 실행하지 않은 요청의 FAILED 완료를 기록한다.
    """

    def __init__(
        self,
        run: Callable,
        ready: Callable,
        model_settings: ModelSettings,
        fail: Callable | None = None,
    ):
        self._run = run
        self._ready = ready
        self._model_settings = model_settings
        self._fail = fail

    def __call__(self, event: IncomingEvent) -> None:
        AppLogger.current().info("graph refresh event handle")
        request, user_id, required_events, wrapped_key = refresh_request(
            event, self._model_settings
        )
        with _classified():
            self._ready(request.job.project_id, required_events)
            self._run(request, user_id, wrapped_key)

    def exhausted(self, event: IncomingEvent, failure: NotReady) -> None:
        """재시도를 다 쓰도록 투영이 준비되지 않은 요청을 FAILED 완료로 끝낸다.

        DLQ로 보내면 완료가 없어 Content의 요청이 진행 중으로 남는다. FAILED면 Content가
        같은 문서를 다음 요청에 다시 싣는다. 기록에 실패하면 예외를 소비자가 DLQ로 보낸다.
        """
        # 이유는 투영 대기 코드(PROJECTION_NOT_READY·GRAPH_NOT_READY)다
        AppLogger.current().warn(
            "graph refresh projection wait exhausted", LogField.error_type(failure.code)
        )
        if self._fail is None:
            raise failure
        request, user_id, _, wrapped_key = refresh_request(event, self._model_settings)
        with _classified():
            self._fail(
                request, user_id, wrapped_key, Failure(failure.code, str(failure))
            )

    def rejected(self, event: IncomingEvent, failure: Exception) -> None:
        """소비자가 이 요청을 DLQ로 보내기 직전에 부른다. 먼저 FAILED 완료를 남겨 Content의 요청을 끝낸다.

        DLQ만 보내면 완료 이벤트가 없어 Content의 요청이 시간 제한(RefreshTimeout)까지 진행 중으로 남는다.
        그래서 의존 실패(invalid:refresh-dependency), 재시도 소진, 투영 대기 처리 실패처럼 요청을 해석한 뒤의 거절은
        고정 코드 PROCESSING_FAILED·고정 문구로 FAILED 완료를 기록한다(InboxEvent의 발행 대기를 relay가 보낸다).

        남기지 않는 경우
        - 충돌(conflict:refresh-request): 같은 요청 ID가 다른 입력으로 이미 있다. 그 요청의 완료를 덮지 않는다
        - 요청으로 해석하지 못한 payload(invalid:refresh-request): 완료를 만들 요청이 없다
        - FAILED 완료를 기록할 처리기가 없는 조립(fail 없음)

        기록이 실패해도 예외를 내보내지 않는다. warn을 남기고 소비자가 그대로 DLQ로 보낸다.
        """
        AppLogger.current().info("graph refresh rejection handle")
        if self._fail is None or (
            isinstance(failure, InvalidEvent) and failure.args == (CONFLICT,)
        ):
            return
        try:
            request, user_id, _, wrapped_key = refresh_request(
                event, self._model_settings
            )
        except InvalidEvent:
            return
        try:
            self._fail(
                request,
                user_id,
                wrapped_key,
                Failure(PROCESSING_FAILED, PROCESSING_FAILED_MESSAGE),
            )
        except Exception as error:
            # 저장소·Inbox 실패. 메시지에는 응답 내용이 섞일 수 있어 종류와 메시지 없는 스택만 남긴다
            AppLogger.current().warn(
                "graph refresh failed completion record failed",
                LogField.error_type(type(error).__name__),
                LogField.safe_stack_trace(error),
            )


@contextmanager
def _classified():
    """refresh 실패를 소비자의 거절·재시도·투영 대기로 나눈다."""
    try:
        yield
    except RequestConflict:
        raise InvalidEvent(CONFLICT) from None
    except RefreshFailure as failure:
        # seed가 그래프에 아직 없으면 투영 대기와 같다. 재시도를 다 쓰면 FAILED로 끝낸다.
        if failure.failure.code == "GRAPH_NOT_READY":
            raise NotReady(
                "GRAPH_NOT_READY", "Relationship projection not ready"
            ) from None
        if failure.failure.retryable:
            raise RetryableProcessingError("Refresh dependency unavailable") from None
        raise InvalidEvent("invalid:refresh-dependency") from None
