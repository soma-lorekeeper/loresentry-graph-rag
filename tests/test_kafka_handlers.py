"""Content 업무 payload 변환·업무 오류 분류를 서버 없이 확인한다."""

from dataclasses import replace

import pytest

from app.kafka.errors import InvalidEvent, RetryableProcessingError
from app.kafka.handlers import (
    ProjectionEventHandler,
    RefreshEventHandler,
    projection_change,
    refresh_request,
)
from app.kafka.messages import parse_event
from app.projection.models import ProjectionUnavailable
from app.refresh.models import DocumentState
from tests.test_kafka_messages import PROJECT, REQUEST, content_record


def file_event(kind="UPDATED"):
    event = parse_event(content_record("FileChanged"))
    payload = {
        **event.payload,
        "change_type": kind,
        "file": {"title": "문서", "folder_code": "CHARACTER", "description": "설명"},
        "relations": [],
    }
    return replace(event, payload=payload)


def request_event():
    event = parse_event(content_record())
    return replace(
        event,
        payload={
            **event.payload,
            "user_id": REQUEST,
            "input_fingerprint": "a" * 64,
            "input": {"bucket": "private-bucket", "key": "refresh/input.json"},
            "files": [{"file_id": REQUEST, "revision_no": 0, "state": "ACTIVE"}],
            "required_events": [],
            "model_settings": {
                "model": "gpt-6-luna",
                "prompt_version": "refresh-prompt-v13",
                "schema_version": "refresh-candidate-v7",
            },
        },
    )


def test_active_and_inactive_projection_snapshots():
    active = projection_change(file_event())
    assert active.files[0].description == "설명"
    assert active.sequence == 2**63 - 1
    assert active.snapshot_file_id == REQUEST
    trash = projection_change(file_event("TRASHED"))
    assert trash.trashed_file_ids == (REQUEST,)
    deleted = projection_change(file_event("DELETED"))
    assert deleted.deleted_file_ids == (REQUEST,)
    with pytest.raises(InvalidEvent):
        projection_change(file_event("UNKNOWN"))
    with pytest.raises(InvalidEvent):
        projection_change(replace(file_event(), payload={}))


def test_refresh_identity_profile_and_manifest():
    request, user, events = refresh_request(request_event())
    assert request.job.project_id == PROJECT and user == REQUEST
    assert request.job.input_fingerprint == "a" * 64
    assert request.changed_documents[0].revision_no == 0
    assert request.changed_documents[0].state == DocumentState.ACTIVE
    assert request.model_settings.prompt_version == "refresh-prompt-v13"
    assert request.seed_ids == (REQUEST,)
    assert events == ()
    event = request_event()
    with pytest.raises(InvalidEvent):
        refresh_request(
            replace(event, payload={**event.payload, "input_fingerprint": "ETag"})
        )
    with pytest.raises(InvalidEvent):
        refresh_request(
            replace(
                event, payload={**event.payload, "files": event.payload["files"] * 2}
            )
        )
    with pytest.raises(InvalidEvent):
        refresh_request(replace(event, payload={**event.payload, "user_id": None}))


def test_projection_unavailable_is_retried_not_silently_committed():
    class Handler:
        def handle(self, *args, **kwargs):
            raise ProjectionUnavailable("private response")

    with pytest.raises(RetryableProcessingError):
        ProjectionEventHandler(Handler())(file_event())


def test_refresh_checks_readiness_before_generation():
    calls = []
    handler = RefreshEventHandler(
        lambda request, user: calls.append("run"),
        lambda project, ids: calls.append("ready"),
    )
    handler(request_event())
    assert calls == ["ready", "run"]

    def not_ready(*args):
        raise RetryableProcessingError("waiting")

    with pytest.raises(RetryableProcessingError):
        RefreshEventHandler(lambda *args: pytest.fail("ran early"), not_ready)(
            request_event()
        )
