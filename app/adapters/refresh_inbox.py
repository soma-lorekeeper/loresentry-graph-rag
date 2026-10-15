"""Neptune writer에 refresh 완료 기록만 보관한다. 실행 잠금·진행 상태는 없다."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from app.adapters.neptune_client import (
    NeptuneGremlinClient,
    NeptuneQueryRejected,
    NeptuneUnavailable,
)
from app.adapters.projection_gremlin import literal
from app.adapters.refresh_schema import StrictObject, json_bytes
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.inbox import CONSUMER_NAME, request_digest, validate_completion
from app.refresh.models import Completion, Failure, RefreshRequest

CONFLICT = "REFRESH_INBOX_CONFLICT"


class Receipt(StrictObject):
    schema_version: Literal["refresh-inbox-v1"]
    completion: Completion


def _identity(request: RefreshRequest) -> tuple[str, str]:
    project, identifier = request.job.project_id, request.job.request_id
    if any(str(UUID(value)) != value for value in (project, identifier)):
        raise ValueError("Refresh Inbox requires canonical project and request UUIDs")
    properties = {
        "consumer_name": CONSUMER_NAME,
        "project_id": project,
        "request_id": identifier,
        "request_sha256": request_digest(request),
    }
    matches = "__.hasLabel('InboxEvent')" + "".join(
        f".has({literal(key)},{literal(value)})" for key, value in properties.items()
    )
    return f"inbox:{CONSUMER_NAME}:{project}:{identifier}", matches


class NeptuneRefreshInbox(NeptuneGremlinClient):
    """Inbox 존재는 결과 저장과 완료 발행 확인이 끝났다는 의미다."""

    def _execute(self, query):
        try:
            return self.execute(query, rejection_codes=(CONFLICT,))
        except NeptuneQueryRejected:
            raise RequestConflict(
                "Refresh Inbox request or completion differs"
            ) from None
        except NeptuneUnavailable:
            raise RefreshFailure(
                Failure(
                    "REFRESH_INBOX_UNAVAILABLE",
                    "Refresh Inbox could not complete",
                    True,
                )
            ) from None

    @staticmethod
    def _invalid():
        raise RefreshFailure(
            Failure("REFRESH_INBOX_INVALID", "Invalid refresh Inbox response", True)
        ) from None

    def find(self, request: RefreshRequest) -> Completion | None:
        identifier, matches = _identity(request)
        result = self._execute(
            f"g.V({literal(identifier)}).choose({matches},__.identity(),"
            f"__.fail('{CONFLICT}')).valueMap('completion_json')"
        )
        if not result:
            return None
        if len(result) != 1 or not isinstance(result[0], dict):
            self._invalid()
        values = result[0].get("completion_json")
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], str)
        ):
            self._invalid()
        try:
            receipt = Receipt.model_validate_json(values[0])
            validate_completion(request, receipt.completion)
        except (ValidationError, RequestConflict):
            self._invalid()
        return receipt.completion

    def complete(self, request: RefreshRequest, completion: Completion) -> None:
        validate_completion(request, completion)
        identifier, matches = _identity(request)
        receipt = Receipt(schema_version="refresh-inbox-v1", completion=completion)
        encoded = json_bytes(receipt.model_dump(mode="json")).decode("utf-8")
        properties = {
            "consumer_name": CONSUMER_NAME,
            "project_id": request.job.project_id,
            "request_id": request.job.request_id,
            "request_sha256": request_digest(request),
            "completion_json": encoded,
            "processed_at": datetime.now(UTC).isoformat(),
        }
        duplicate = (
            f"__.unfold().choose({matches}.has('completion_json',{literal(encoded)}),"
            f"__.constant('DUPLICATE'),__.fail('{CONFLICT}'))"
        )
        inserted = (
            f"__.addV('InboxEvent').property(T.id,{literal(identifier)})"
            + "".join(
                f".property(single,{literal(key)},{literal(value)})"
                for key, value in properties.items()
            )
            + ".constant('APPLIED')"
        )
        result = self._execute(
            f"g.V({literal(identifier)}).fold().coalesce({duplicate},{inserted})"
        )
        if result not in (["APPLIED"], ["DUPLICATE"]):
            self._invalid()
