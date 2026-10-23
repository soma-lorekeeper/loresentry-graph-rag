"""S3의 내부 입력·요청·실행 스냅샷 JSON 계약. Content 운영 합의와는 구분한다."""

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.adapters.content_schema import ContentDocument
from app.refresh.models import (
    ExecutionSnapshot,
    InputSnapshot,
    RefreshRequest,
    RefreshResult,
)


class StrictObject(BaseModel):
    """중첩 dataclass까지 엄격한 자료형과 알려진 필드만 허용한다."""

    model_config = ConfigDict(strict=True, extra="forbid")


class InputObject(StrictObject):
    """Content가 요청마다 올린 원문(refresh-input-v2). fingerprint는 바이트로 계산한다.

    요청 식별은 싣지 않는다. 이벤트의 key 경로와 input_fingerprint가 이 바이트를 요청에 묶는다.
    """

    schema_version: Literal["refresh-input-v2"]
    documents: tuple[ContentDocument, ...]


class RequestObject(StrictObject):
    """같은 요청 ID에 다른 입력·설정을 사용할 수 없도록 저장하는 불변 manifest."""

    schema_version: Literal["refresh-request-v1"]
    request: RefreshRequest


class ContextObject(StrictObject):
    """재실행에서 최초 입력·추가 조회 문서를 재사용하는 실행 스냅샷."""

    schema_version: Literal["refresh-context-v1"]
    snapshot: ExecutionSnapshot


class TypedResult(StrictObject):
    """결과 역직렬화 뒤 중첩 내부 값의 자료형도 검사한다."""

    result: RefreshResult


def json_bytes(value) -> bytes:
    """본문을 정규화하지 않고 결정적 UTF-8 JSON 바이트를 만든다."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def input_bytes(source: InputSnapshot) -> bytes:
    """Content 대신 테스트·평가가 저장할 v2 JSON. 이 바이트의 SHA-256을 요청에 전달한다."""
    return json_bytes(
        {
            "schema_version": "refresh-input-v2",
            "documents": [
                {
                    "id": d.document_id,
                    "revision_no": d.revision_no,
                    "folder_code": d.folder_code,
                    "title": d.title,
                    "body_text": d.body_text,
                    "properties": [{"key": k, "value": v} for k, v in d.properties],
                    "relations": [
                        {
                            "target_document_id": r.target_document_id,
                            "relation_key": r.relation_key,
                            "description": r.description,
                        }
                        for r in d.relations
                    ],
                }
                for d in source.documents
            ],
        }
    )


def context_bytes(snapshot: ExecutionSnapshot) -> bytes:
    """과거에 없던 선택 제목은 생략하여 기존 불변 실행 스냅샷의 바이트를 보존한다."""
    obj = ContextObject(schema_version="refresh-context-v1", snapshot=snapshot)
    value = obj.model_dump(mode="json")
    omit_absent_titles(value["snapshot"]["documents"])
    omit_default_discovery(value["snapshot"]["request"])
    return json_bytes(value)


def omit_default_discovery(request: dict) -> None:
    """과거 선택 정책의 기본값은 저장 바이트에 새 필드를 추가하지 않는다."""
    settings = request["settings"]
    if settings.get("separate_discovery") is False:
        settings.pop("separate_discovery", None)
    if (
        type(settings.get("chunk_overlap_chars")) is int
        and settings["chunk_overlap_chars"] == 0
    ):
        settings.pop("chunk_overlap_chars", None)


def omit_absent_titles(documents: list[dict]) -> None:
    """미제공 제목만 생략한다. 비활성 body_text 등 기존 null 필드는 그대로 둔다."""
    for document in documents:
        if document.get("title") is None:
            document.pop("title", None)


def fingerprint(body: bytes) -> str:
    """S3에 저장된 원본 바이트 전체의 SHA-256이다. ETag나 문서 revision과 다르다."""
    return sha256(body).hexdigest()
