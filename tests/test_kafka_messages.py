"""Content main의 wire 규약과 GraphRAG 완료·DLQ 포장을 확인한다."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.kafka.errors import InvalidEvent
from app.kafka.messages import (
    PROJECT_CHANGED,
    REFRESH_REQUESTED,
    completion_record,
    dead_letter_record,
    parse_event,
)
from app.kafka.polling import Record
from app.refresh.models import ArtifactRef, Completion, Failure, JobKey, Outcome

PROJECT = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"
EVENT = "33333333-3333-4333-8333-333333333333"


def content_record(event_type="GraphRefreshRequested"):
    subject_field = {
        "GraphRefreshRequested": "request_id",
        "FileChanged": "file_id",
        "ProjectDeleted": "project_id",
    }[event_type]
    subject = PROJECT if subject_field == "project_id" else REQUEST
    payload = {"project_id": PROJECT, subject_field: subject, "future_field": True}
    headers = {
        "ce_specversion": "1.0",
        "ce_id": EVENT,
        "ce_type": event_type,
        "ce_source": "/content",
        "ce_subject": subject,
        "ce_time": "2026-10-16T10:00:00.123Z",
        "ce_sequence": "9223372036854775807",
        "content-type": "Application/JSON; charset=UTF-8",
    }
    return Record(
        REFRESH_REQUESTED if event_type == "GraphRefreshRequested" else PROJECT_CHANGED,
        5,
        2**40,
        PROJECT.encode(),
        json.dumps(payload).encode(),
        tuple((name, value.encode()) for name, value in headers.items()),
    )


def with_header(record, name, value):
    return replace(
        record,
        headers=tuple((k, v) for k, v in record.headers if k != name)
        + (() if value is None else ((name, value),)),
    )


@pytest.mark.parametrize(
    "kind", ["FileChanged", "ProjectDeleted", "GraphRefreshRequested"]
)
def test_content_headers_key_and_subject(kind):
    event = parse_event(content_record(kind))
    assert event.event_id == EVENT
    assert event.project_id == PROJECT
    assert event.event_type == kind
    assert event.payload["future_field"] is True
    assert event.sequence == (None if kind == "GraphRefreshRequested" else 2**63 - 1)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ce_id", None),
        ("ce_type", b" "),
        ("ce_specversion", b"2.0"),
        ("ce_id", b"not-a-uuid"),
        ("ce_source", b"/auth"),
        ("ce_subject", PROJECT.encode()),
        ("ce_time", b"2026-10-16T10:00:00"),
        ("ce_time", b"2026-13-16T10:00:00Z"),
        ("ce_time", b"2026-10-16T100000Z"),
        ("content-type", b"text/plain"),
        ("ce_id", b"\xff"),
    ],
)
def test_bad_known_headers_are_rejected_without_input_values(name, value):
    with pytest.raises(InvalidEvent) as error:
        parse_event(with_header(content_record(), name, value))
    if value:
        assert value.decode(errors="replace") not in str(error.value)


def test_duplicate_header_rejected_but_unknown_type_skips_before_other_validation():
    record = content_record()
    with pytest.raises(InvalidEvent):
        parse_event(
            replace(record, headers=record.headers + (("ce_id", EVENT.encode()),))
        )
    record = replace(record, headers=(("ce_type", b"FutureEvent"),), value=b"not JSON")
    assert parse_event(record) is None


@pytest.mark.parametrize(
    "value", [None, b"null", b"[]", b"{", b"\xff", b'{"x":NaN}', b'{"x":1,"x":2}']
)
def test_bad_json_rejected(value):
    with pytest.raises(InvalidEvent):
        parse_event(replace(content_record(), value=value))


def test_key_and_payload_subject_must_match():
    with pytest.raises(InvalidEvent, match="project-key"):
        parse_event(replace(content_record(), key=EVENT.encode()))
    record = content_record()
    payload = json.loads(record.value)
    payload["request_id"] = PROJECT
    with pytest.raises(InvalidEvent, match="ce_subject"):
        parse_event(replace(record, value=json.dumps(payload).encode()))


@pytest.mark.parametrize(
    "value", [None, b"0", b"-1", b"1.0", b"+1", b"9223372036854775808", b"9" * 100]
)
def test_sequence_bounds(value):
    with pytest.raises(InvalidEvent, match="ce_sequence"):
        parse_event(with_header(content_record("FileChanged"), "ce_sequence", value))


def test_completion_id_stable_and_content_parser_contract():
    completion = Completion(
        JobKey(PROJECT, REQUEST, "a" * 64),
        ArtifactRef("private", "refresh/result.json"),
        Outcome.PROPOSED,
        prompt_version="refresh-prompt-v13",
    )
    first = completion_record(completion, now=datetime(2026, 10, 16, tzinfo=UTC))
    second = completion_record(completion, now=datetime(2026, 10, 17, tzinfo=UTC))
    headers = dict(first.headers)
    assert headers["ce_id"] == dict(second.headers)["ce_id"]
    assert UUID(headers["ce_id"].decode()).version == 5
    assert headers["ce_subject"] == REQUEST.encode()
    assert headers["ce_source"] == b"/graph-rag"
    assert first.key == PROJECT.encode()
    assert headers["ce_time"] == b"2026-10-16T00:00:00.000Z"
    payload = json.loads(first.value)
    assert payload["outcome"] == "SUCCEEDED"
    assert payload["error"] is None
    assert payload["result"] == {"bucket": "private", "key": "refresh/result.json"}
    assert set(payload) == {
        "request_id",
        "project_id",
        "outcome",
        "result",
        "error",
        "prompt_version",
    }
    failed = replace(
        completion, outcome=Outcome.FAILED, failure=Failure("FAILED", "Safe")
    )
    assert (
        json.loads(completion_record(failed, now=datetime.now(UTC)).value)["outcome"]
        == "FAILED"
    )
    with pytest.raises(ValueError):
        completion_record(replace(completion, result_ref=None), now=datetime.now(UTC))


def test_dlq_preserves_original_bytes_and_replaces_diagnostics():
    record = content_record("FileChanged")
    record = replace(
        record,
        headers=record.headers + (("kafka_dlt-exception-message", b"old secret"),),
    )
    output = dead_letter_record(record, ValueError("secret manuscript"))
    assert output.topic == PROJECT_CHANGED + ".graph-rag.dlq"
    assert (output.key, output.value) == (record.key, record.value)
    headers = dict(output.headers)
    assert headers["ce_id"] == EVENT.encode()
    assert (
        int.from_bytes(headers["kafka_dlt-original-partition"], "big", signed=True) == 5
    )
    assert (
        int.from_bytes(headers["kafka_dlt-original-offset"], "big", signed=True)
        == 2**40
    )
    assert headers["kafka_dlt-exception-message"] == b"builtins.ValueError"
    assert b"secret" not in b"".join(value or b"" for _, value in output.headers)
