"""Neptune writer의 한 Inbox vertex에 처리 완료와 Outbox 발행 대기를 기록한다."""

import re
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
from app.logs import AppLogger, LogField, skip_entry_log, traceparent
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.inbox import (
    CONSUMER_NAME,
    PendingCompletion,
    request_digest,
    validate_completion,
)
from app.refresh.models import Completion, Failure, RefreshRequest

# 완료 Inbox를 Neptune에 읽고 쓰는 repository 레이어다(log.layer)
LOG_LAYER = "repository"
CONFLICT = "REFRESH_INBOX_CONFLICT"
# InboxEvent에 저장하는 traceparent 모양(W3C version 00). 다른 값은 저장된 trace로 쓰지 않는다
TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


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
        AppLogger.current().info("refresh inbox find")
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
        """완료와 발행 대기를 InboxEvent 하나로 기록한다.

        지금 요청 처리(Kafka 레코드 흐름의 consumer span)의 traceparent를 traceparent 속성으로 함께 저장한다.
        relay가 그 값을 부모로 행의 범위를 열고 그 안에서 완료 이벤트를 보내 content가 이 요청의 trace를 잇는다.
        요청 식별(request_sha256)과 중복 판정(completion_json)에는 들어가지 않으므로, 재전달된 같은 완료는 처음 저장한
        traceparent를 유지한다.
        """
        AppLogger.current().info(
            "refresh inbox complete",
            LogField.param("refresh_outcome", completion.outcome),
        )
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
        current = traceparent()
        if current is not None:
            # 추적 중이 아니면(span 없음) 속성을 만들지 않는다
            properties["traceparent"] = current
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
        # APPLIED: 새로 기록함, DUPLICATE: 같은 완료가 이미 있음(발행 대기를 다시 켜지 않는다)
        AppLogger.current().info(
            "refresh inbox completed",
            LogField.result_code("inbox", result[0], {"APPLIED", "DUPLICATE"}),
        )

    @skip_entry_log("called every second by the outbox relay; rounds log only rows")
    def pending(self, limit: int = 50) -> list[PendingCompletion]:
        """발행 대기를 오래된 순으로 읽는다. traceparent가 없는 행(추적 전에 기록한 완료)은 None이다."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid Outbox batch size")
        rows = self._execute(
            "g.V().hasLabel('InboxEvent')"
            f".has('consumer_name',{literal(CONSUMER_NAME)})"
            ".has('outbox_pending','true').order().by('processed_at')"
            f".limit({limit})"
            ".project('identifier','receipt','created_at','traceparent')"
            ".by(id()).by('completion_json').by('processed_at')"
            ".by(__.coalesce(__.values('traceparent'),__.constant('')))"
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
                stored = row.get("traceparent")
                entries.append(
                    PendingCompletion(
                        expected,
                        completion,
                        created_at,
                        stored
                        if isinstance(stored, str) and TRACEPARENT.fullmatch(stored)
                        else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                self._invalid()
        return entries

    def mark_sent(self, entry: PendingCompletion) -> None:
        AppLogger.current().info("refresh outbox mark sent")
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
