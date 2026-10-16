"""Content 규약의 wire 포장만 해석한다. 업무 payload 변환은 주입한 처리기가 맡는다."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.kafka.errors import InvalidEvent
from app.kafka.polling import Record
from app.refresh.models import Completion
from app.refresh.serialization import completion_to_payload

PROJECT_CHANGED = "content.project.changed.v1"
REFRESH_REQUESTED = "content.graph-refresh.requested.v1"
REFRESH_COMPLETED = "graph-rag.graph-refresh.completed.v1"
EVENT_TYPES = {
    PROJECT_CHANGED: frozenset({"FileChanged", "ProjectDeleted"}),
    REFRESH_REQUESTED: frozenset({"GraphRefreshRequested"}),
}
COMPLETION_NAMESPACE = uuid5(NAMESPACE_URL, "urn:loresentry:graph-refresh-completed:v1")


@dataclass(frozen=True)
class OutgoingRecord:
    topic: str
    key: bytes | None
    value: bytes | None
    headers: tuple[tuple[str, bytes | None], ...]


@dataclass(frozen=True)
class IncomingEvent:
    event_id: str
    event_type: str
    project_id: str
    subject: str
    sequence: int | None
    payload: dict[str, Any]


def _uuid(value: Any, field: str) -> str:
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError
        normalized = str(UUID(value))
        if normalized != value.lower():
            raise ValueError
        return normalized
    except ValueError:
        raise InvalidEvent(f"invalid:{field}") from None


def _text(value: bytes | None, field: str) -> str:
    try:
        if value is None:
            raise ValueError
        result = value.decode("utf-8")
        if not result.strip():
            raise ValueError
        return result
    except (UnicodeError, ValueError):
        raise InvalidEvent(f"invalid:{field}") from None


def _header(record: Record, name: str) -> str:
    values = [value for key, value in record.headers if key == name]
    if len(values) != 1:
        raise InvalidEvent(f"invalid:{name}")
    return _text(values[0], name)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvent("duplicate:json-field")
        result[key] = value
    return result


def _constant(_):
    raise InvalidEvent("invalid:json-number")


def parse_event(record: Record) -> IncomingEvent | None:
    """미지원 종류는 None, 알려진 종류의 계약 위반은 안전한 InvalidEvent로 반환한다."""
    if record.topic not in EVENT_TYPES:
        raise ValueError("Unsupported business topic")
    event_type = _header(record, "ce_type")
    if event_type not in EVENT_TYPES[record.topic]:
        return None
    if _header(record, "ce_specversion") != "1.0":
        raise InvalidEvent("invalid:ce_specversion")
    event_id = _uuid(_header(record, "ce_id"), "ce_id")
    if _header(record, "ce_source") != "/content":
        raise InvalidEvent("invalid:ce_source")
    subject = _uuid(_header(record, "ce_subject"), "ce_subject")
    timestamp = _header(record, "ce_time")
    try:
        # Require the RFC3339 time separator and an explicit offset.
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
            timestamp,
        ):
            raise ValueError
        parsed = datetime.fromisoformat(timestamp.upper().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise InvalidEvent("invalid:ce_time") from None
    media_type = _header(record, "content-type").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise InvalidEvent("invalid:content-type")
    sequence = None
    if record.topic == PROJECT_CHANGED:
        raw = _header(record, "ce_sequence")
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
            raise InvalidEvent("invalid:ce_sequence")
        sequence = int(raw)
        if not 0 < sequence <= 2**63 - 1:
            raise InvalidEvent("invalid:ce_sequence")
    try:
        payload = json.loads(
            _text(record.value, "payload"),
            object_pairs_hook=_object,
            parse_constant=_constant,
        )
    except (ValueError, RecursionError):
        raise InvalidEvent("invalid:payload") from None
    if not isinstance(payload, dict):
        raise InvalidEvent("invalid:payload")
    project_id = _uuid(payload.get("project_id"), "project_id")
    if _uuid(_text(record.key, "key"), "key") != project_id:
        raise InvalidEvent("mismatch:project-key")
    subject_field = {
        "FileChanged": "file_id",
        "ProjectDeleted": "project_id",
        "GraphRefreshRequested": "request_id",
    }[event_type]
    if _uuid(payload.get(subject_field), subject_field) != subject:
        raise InvalidEvent("mismatch:ce_subject")
    return IncomingEvent(event_id, event_type, project_id, subject, sequence, payload)


def completion_record(completion: Completion, *, now: datetime) -> OutgoingRecord:
    """동일 요청의 재발행은 UUIDv5 ID를 유지한다. 시간은 IO 계층에서 주입한다."""
    project_id = _uuid(completion.job.project_id, "project_id")
    request_id = _uuid(completion.job.request_id, "request_id")
    if now.tzinfo is None:
        raise ValueError("Completion time must be timezone-aware")
    event_id = uuid5(COMPLETION_NAMESPACE, f"{project_id}:{request_id}")
    headers = {
        "ce_specversion": "1.0",
        "ce_id": str(event_id),
        "ce_type": "GraphRefreshCompleted",
        "ce_source": "/graph-rag",
        "ce_subject": request_id,
        "ce_time": now.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "content-type": "application/json",
    }
    return OutgoingRecord(
        REFRESH_COMPLETED,
        project_id.encode(),
        json.dumps(
            completion_to_payload(completion), ensure_ascii=False, allow_nan=False
        ).encode("utf-8"),
        tuple((key, value.encode()) for key, value in headers.items()),
    )


def dead_letter_record(record: Record, failure: Exception) -> OutgoingRecord:
    """원본 bytes를 보존하고 실패 진단에 예외 메시지·본문을 복사하지 않는다."""
    if record.topic not in EVENT_TYPES:
        raise ValueError("Unsupported business topic")
    reason = f"{type(failure).__module__}.{type(failure).__qualname__}"
    headers = tuple(
        (name, value)
        for name, value in record.headers
        if not name.startswith("kafka_dlt-")
    ) + (
        ("kafka_dlt-original-topic", record.topic.encode()),
        (
            "kafka_dlt-original-partition",
            record.partition.to_bytes(4, "big", signed=True),
        ),
        ("kafka_dlt-original-offset", record.offset.to_bytes(8, "big", signed=True)),
        ("kafka_dlt-exception-fqcn", reason.encode()),
        # Even arbitrary external exception messages must never reach the DLQ headers.
        ("kafka_dlt-exception-message", reason.encode()),
    )
    return OutgoingRecord(
        record.topic + ".graph-rag.dlq", record.key, record.value, headers
    )
