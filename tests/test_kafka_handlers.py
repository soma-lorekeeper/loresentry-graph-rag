"""Content 업무 payload 변환·업무 오류 분류를 서버 없이 확인한다."""

from dataclasses import replace
from uuid import uuid4

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
from app.refresh.models import DocumentState, ModelSettings
from tests.test_kafka_messages import EVENT, PROJECT, REQUEST, content_record

LOW = "11111111-1111-4111-8111-1111111111aa"
HIGH = "33333333-3333-4333-8333-3333333333aa"


def file_event(kind="UPDATED", state="ACTIVE", relations=()):
    event = parse_event(content_record("FileChanged"))
    payload = {
        **event.payload,
        "change_type": kind,
        "changed_fields": ["TITLE"],
        "file": {
            "title": "문서",
            "folder_code": "CHARACTER",
            "episode_id": None,
            "state": state,
            "body_text": "본문",
            "properties": [
                {"key": "alias", "value": "별칭"},
                {"key": "description", "value": "설명"},
                {"key": "description", "value": "둘째 설명"},
            ],
        },
        "relations": [
            {"relation_id": EVENT, "target_document_id": target, "description": "등장"}
            for target in relations
        ],
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
            "files": [{"file_id": REQUEST, "revision_no": 0}],
            "required_events": [],
        },
    )


SETTINGS = ModelSettings(model="gpt-6-luna")


def test_active_and_inactive_projection_snapshots():
    active = projection_change(file_event(relations=(HIGH,)))
    assert active.files[0].description == "설명"
    assert active.files[0].trashed is False
    assert active.sequence == 2**63 - 1
    assert active.snapshot_file_id == REQUEST
    assert [(r.low_document_id, r.high_document_id) for r in active.relations] == [
        (REQUEST, HIGH)
    ]
    # 휴지통 이동도 관계를 그대로 실은 전체 스냅샷이다.
    trash = projection_change(file_event("TRASHED", "TRASHED", relations=(HIGH,)))
    assert trash.files[0].trashed is True and len(trash.relations) == 1
    # 휴지통 문서의 UPDATED는 휴지통 상태를 유지한다.
    assert projection_change(file_event("UPDATED", "TRASHED")).files[0].trashed
    event = file_event("DELETED")
    deleted = projection_change(
        replace(event, payload={**event.payload, "file": None, "relations": None})
    )
    assert deleted.deleted_file_ids == (REQUEST,) and deleted.files == ()
    with pytest.raises(InvalidEvent):
        projection_change(file_event("UNKNOWN"))
    with pytest.raises(InvalidEvent):
        projection_change(replace(file_event(), payload={}))
    with pytest.raises(InvalidEvent):
        projection_change(file_event(state="DELETED"))


def test_projection_rejects_relations_owned_by_the_other_document():
    event = file_event()
    no_description = {**event.payload["file"], "properties": []}
    assert (
        projection_change(
            replace(event, payload={**event.payload, "file": no_description})
        )
        .files[0]
        .description
        == ""
    )
    # Content는 관계를 low 문서 이벤트에만 싣는다. high 쪽에서 온 관계는 계약 위반이다.
    with pytest.raises(InvalidEvent):
        projection_change(file_event(relations=(LOW,)))
    with pytest.raises(InvalidEvent):
        projection_change(file_event(relations=(REQUEST,)))


def test_refresh_identity_profile_and_manifest():
    request, user, events = refresh_request(request_event(), SETTINGS)
    assert request.job.project_id == PROJECT and user == REQUEST
    assert request.job.input_fingerprint == "a" * 64
    assert request.changed_documents[0].revision_no == 0
    assert request.changed_documents[0].state == DocumentState.ACTIVE
    # 모델 설정은 이벤트가 아니라 GraphRAG 기동 설정에서 온다.
    assert request.model_settings == SETTINGS
    assert request.model_settings.prompt_version == "refresh-prompt-v13"
    assert request.seed_ids == (REQUEST,)
    assert events == ()
    event = request_event()
    # 20개를 넘는 요청도 이벤트 단계에서 거절하지 않는다.
    many = [{"file_id": str(uuid4()), "revision_no": 1} for _ in range(25)]
    assert (
        len(
            refresh_request(
                replace(event, payload={**event.payload, "files": many}), SETTINGS
            )[0].seed_ids
        )
        == 25
    )
    for change in (
        {"input_fingerprint": "ETag"},
        {"files": event.payload["files"] * 2},
        {"files": []},
        {"user_id": None},
    ):
        with pytest.raises(InvalidEvent):
            refresh_request(
                replace(event, payload={**event.payload, **change}), SETTINGS
            )


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
        SETTINGS,
    )
    handler(request_event())
    assert calls == ["ready", "run"]

    def not_ready(*args):
        raise RetryableProcessingError("waiting")

    with pytest.raises(RetryableProcessingError):
        RefreshEventHandler(
            lambda *args: pytest.fail("ran early"), not_ready, SETTINGS
        )(request_event())
