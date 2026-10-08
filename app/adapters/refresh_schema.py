"""S3의 내부 입력·요청·실행 스냅샷 JSON 계약. Content 운영 합의와는 구분한다."""

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.refresh.models import (
    DocumentSnapshot,
    ExecutionSnapshot,
    InputSnapshot,
    RefreshRequest,
    RefreshResult,
)


class StrictObject(BaseModel):
    """중첩 dataclass까지 엄격한 자료형과 알려진 필드만 허용한다."""

    model_config = ConfigDict(strict=True, extra="forbid")


class InputObject(StrictObject):
    """요청 식별과 최초 전달 원문. fingerprint는 객체 바이트로 별도 계산한다."""

    schema_version: Literal["refresh-input-v1"]
    project_id: str
    request_id: str
    documents: tuple[DocumentSnapshot, ...]


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
    """입력 공급자가 저장할 JSON. 저장할 이 바이트의 SHA-256을 요청에 전달한다."""
    obj = InputObject(
        schema_version="refresh-input-v1",
        project_id=source.job.project_id,
        request_id=source.job.request_id,
        documents=source.documents,
    )
    return json_bytes(obj.model_dump(mode="json"))


def fingerprint(body: bytes) -> str:
    """S3에 저장된 원본 바이트 전체의 SHA-256이다. ETag나 문서 revision과 다르다."""
    return sha256(body).hexdigest()
