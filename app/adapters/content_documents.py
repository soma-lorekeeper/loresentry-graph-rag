"""기존 Content API로 활성 프로젝트의 추가 문서 본문을 조회한다."""

import math
import os
from typing import Never, Self
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.adapters.content_schema import ContentResponse, ProjectResponse, canonical_id
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentBatch, Failure


def _base_url(value: str) -> str:
    url = urlsplit(value)
    if (
        value != value.strip()
        or url.scheme not in {"http", "https"}
        or not url.hostname
        or any(char.isspace() for char in value)
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("CONTENT_BASE_URL must be an HTTP(S) service origin")
    _ = url.port
    return value.rstrip("/")


def _fail(code: str, message: str, retryable: bool = False) -> Never:
    raise RefreshFailure(Failure(code, message, retryable)) from None


class ContentDocumentSource:
    """신뢰한 호출자 ID를 한 실행에 바인딩한다. 재시도는 RefreshService가 담당한다."""

    def __init__(
        self,
        base_url: str,
        *,
        user_id: str,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = _base_url(base_url)
        self.user_id = canonical_id(user_id)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Content timeout must be positive")
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(3.0, timeout_seconds)
        )
        self._owned_client = client is None
        self._client = client if client is not None else httpx.Client()

    @classmethod
    def from_env(cls, *, user_id: str, base_url: str | None = None) -> Self:
        return cls(
            base_url
            if base_url is not None
            else os.environ.get("CONTENT_BASE_URL", ""),
            user_id=user_id,
            timeout_seconds=float(os.environ.get("CONTENT_TIMEOUT_SECONDS", "10")),
        )

    def fetch_documents(
        self, project_id: str, document_ids: tuple[str, ...]
    ) -> DocumentBatch:
        try:
            canonical_id(project_id)
            ids = tuple(dict.fromkeys(canonical_id(value) for value in document_ids))
        except (ValueError, TypeError, AttributeError):
            _fail(
                "INVALID_DOCUMENT_REQUEST",
                "Canonical project and document IDs required",
            )
        if not ids:
            return DocumentBatch(())

        # 문서 API는 소유권과 문서 휴지통만 검사하므로 활성 프로젝트도 먼저 확인한다.
        response = self._get(f"/projects/{project_id}")
        try:
            project = ProjectResponse.model_validate_json(response.content)
        except ValidationError:
            _fail("CONTENT_RESPONSE_INVALID", "Invalid Content project response")
        if project.id != project_id:
            _fail("PROJECT_MISMATCH", "Content project differs from request")
        if project.trashed_at is not None:
            _fail("CONTENT_PROJECT_UNAVAILABLE", "Content project is not active")

        found, missing = [], []
        for document_id in ids:
            response = self._get(f"/files/{document_id}/content", document=True)
            if response is None:
                missing.append(document_id)
                continue
            try:
                document = ContentResponse.model_validate_json(response.content)
            except ValidationError:
                _fail("CONTENT_RESPONSE_INVALID", "Invalid Content document response")
            if document.project_id != project_id:
                _fail("PROJECT_MISMATCH", "Content document belongs to another project")
            if document.id != document_id:
                _fail("FETCH_MISMATCH", "Content document differs from requested ID")
            found.append(document.snapshot())
        return DocumentBatch(tuple(found), tuple(missing))

    def _get(self, path: str, *, document: bool = False) -> httpx.Response | None:
        try:
            response = self._client.get(
                f"{self.base_url}{path}",
                headers={"X-User-Id": self.user_id, "Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            _fail("CONTENT_UNAVAILABLE", "Content request could not complete", True)
        status = response.status_code
        if status == 200:
            return response
        if status in {401, 403}:
            _fail("CONTENT_ACCESS_DENIED", "Content denied the caller")
        if status in {408, 429} or status >= 500:
            _fail("CONTENT_UNAVAILABLE", "Content is temporarily unavailable", True)
        if status == 404:
            try:
                payload = response.json()
                code = payload.get("code") if isinstance(payload, dict) else None
            except ValueError:
                code = None
            if document and code == "FILE_NOT_FOUND":
                return None
            if not document and code == "PROJECT_NOT_FOUND":
                _fail("CONTENT_PROJECT_UNAVAILABLE", "Content project is unavailable")
        _fail("CONTENT_REQUEST_REJECTED", "Content rejected the request")

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
