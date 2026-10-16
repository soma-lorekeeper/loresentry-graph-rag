"""Neptune writer의 한 Inbox vertex에 처리 완료와 Outbox 발행 대기를 기록한다."""

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
from app.refresh.inbox import (
    CONSUMER_NAME,
    PendingCompletion,
    request_digest,
    validate_completion,
)
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
    """Inbox 존재는 결과 저장·발행 대기 기록 완료다. Kafka ack와는 독립적이다."""

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
            "outbox_pending": "true",
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

    def pending(self, limit: int = 50) -> list[PendingCompletion]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid Outbox batch size")
        rows = self._execute(
            "g.V().hasLabel('InboxEvent')"
            f".has('consumer_name',{literal(CONSUMER_NAME)})"
            ".has('outbox_pending','true').order().by('processed_at')"
            f".limit({limit}).project('identifier','receipt','created_at')"
            ".by(id()).by('completion_json').by('processed_at')"
        )
        entries = []
        for row in rows:
            try:
                receipt = Receipt.model_validate_json(row["receipt"])
                completion = receipt.completion
                expected = (
                    f"inbox:{CONSUMER_NAME}:"
                    f"{completion.job.project_id}:{completion.job.request_id}"
                )
                created_at = datetime.fromisoformat(row["created_at"])
                if (
                    row["identifier"] != expected
                    or completion.result_ref is None
                    or created_at.utcoffset() is None
                ):
                    self._invalid()
                entries.append(PendingCompletion(expected, completion, created_at))
            except (KeyError, TypeError, ValueError):
                self._invalid()
        return entries

    def mark_sent(self, entry: PendingCompletion) -> None:
        encoded = json_bytes(
            Receipt(
                schema_version="refresh-inbox-v1", completion=entry.completion
            ).model_dump(mode="json")
        ).decode("utf-8")
        result = self._execute(
            f"g.V({literal(entry.identifier)}).hasLabel('InboxEvent')"
            f".has('consumer_name',{literal(CONSUMER_NAME)})"
            f".has('completion_json',{literal(encoded)})"
            f".has('processed_at',{literal(entry.created_at.isoformat())})"
            ".property(single,'outbox_pending','false').constant('SENT')"
        )
        if result != ["SENT"]:
            self._invalid()
