"""Kafka wire schema와 분리된, 검증된 그래프 변경 묶음."""

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

CONSUMER_NAME = "content-graph-projection"


def canonical_uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Expected a canonical lowercase UUID")
    return value


def utf8_text(value: str) -> str:
    value.encode("utf-8")
    return value


Identifier = Annotated[StrictStr, AfterValidator(canonical_uuid)]
Text = Annotated[StrictStr, AfterValidator(utf8_text)]
FolderCode = Literal[
    "WORLDVIEW", "CHARACTER", "LOCATION", "MANUSCRIPT", "ORGANIZATION", "ITEM", "EVENT"
]


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileState(ProjectionModel):
    id: Identifier
    title: Annotated[Text, Field(min_length=1, max_length=255)]
    folder_code: FolderCode
    description: Text = ""
    trashed: StrictBool = False

    @model_validator(mode="after")
    def nonblank_title(self) -> Self:
        if not self.title.strip():
            raise ValueError("File title cannot be blank")
        return self


class RelationState(ProjectionModel):
    id: Identifier
    low_document_id: Identifier
    high_document_id: Identifier
    description: Text = ""

    @model_validator(mode="after")
    def ordered_pair(self) -> Self:
        if UUID(self.low_document_id).int >= UUID(self.high_document_id).int:
            raise ValueError("Use Content's low_document_id < high_document_id pair")
        return self


class ProjectionChange(ProjectionModel):
    """원본 트랜잭션 하나의 내부 변경. Kafka의 확정 payload 스키마는 아니다."""

    event_id: Identifier
    project_id: Identifier
    source: Literal["/content"] = "/content"
    event_type: Literal["FileChanged", "ProjectDeleted"] = "FileChanged"
    files: tuple[FileState, ...] = ()
    relations: tuple[RelationState, ...] = ()
    deleted_file_ids: tuple[Identifier, ...] = ()
    deleted_relation_ids: tuple[Identifier, ...] = ()
    sequence: Annotated[StrictInt, Field(gt=0, le=2**63 - 1)] | None = None
    snapshot_file_id: Identifier | None = None
    trashed_file_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def consistent_batch(self) -> Self:
        if self.sequence is None and (self.snapshot_file_id or self.trashed_file_ids):
            raise ValueError("Snapshot state requires a sequence")
        if self.event_type == "ProjectDeleted" and self.snapshot_file_id is not None:
            raise ValueError("Project deletion has no document subject")
        if self.sequence is not None and self.event_type == "FileChanged":
            if self.snapshot_file_id is None:
                raise ValueError("A sequenced snapshot requires a subject")
            targets = (
                [file.id for file in self.files]
                + list(self.deleted_file_ids)
                + list(self.trashed_file_ids)
            )
            if targets != [self.snapshot_file_id] or self.deleted_relation_ids:
                raise ValueError("A sequenced event changes exactly one document")
            if not self.files and self.relations:
                raise ValueError("Inactive snapshots do not carry relation upserts")
            if any(
                self.snapshot_file_id not in (r.low_document_id, r.high_document_id)
                for r in self.relations
            ):
                raise ValueError("Snapshot relations must touch the subject")
        file_ids = [file.id for file in self.files]
        relation_ids = [relation.id for relation in self.relations]
        pairs = [
            (relation.low_document_id, relation.high_document_id)
            for relation in self.relations
        ]
        for values in (
            file_ids,
            relation_ids,
            pairs,
            self.deleted_file_ids,
            self.deleted_relation_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate IDs or relation pairs in one change")
        if set(file_ids) & set(self.deleted_file_ids):
            raise ValueError("A file cannot be upserted and deleted together")
        if set(relation_ids) & set(self.deleted_relation_ids):
            raise ValueError("A relation cannot be upserted and deleted together")
        for low, high in pairs:
            if {low, high} & set(self.deleted_file_ids):
                raise ValueError("An upserted relation cannot reference a deleted file")
        if self.event_type == "ProjectDeleted" and any(
            (
                self.files,
                self.relations,
                self.deleted_file_ids,
                self.deleted_relation_ids,
                self.trashed_file_ids,
            )
        ):
            raise ValueError("ProjectDeleted carries only project and event metadata")
        return self

    @property
    def inbox_id(self) -> str:
        return f"inbox:{CONSUMER_NAME}:{self.event_id}"

    @property
    def payload_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        if self.sequence is None:
            for field in ("sequence", "snapshot_file_id", "trashed_file_ids"):
                payload.pop(field)
        normalized = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ApplyResult(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"


class ProjectionError(Exception):
    """반영 성공을 확인하지 못해 수신 측이 ack하면 안 되는 실패."""


class ProjectionRejected(ProjectionError):
    """입력 또는 저장 데이터의 계약 위반. 원인을 수정한 뒤 재시도한다."""


class ProjectionUnavailable(ProjectionError):
    """통신·충돌·불명확한 커밋 결과. 같은 이벤트 ID로 전체를 재시도한다."""
