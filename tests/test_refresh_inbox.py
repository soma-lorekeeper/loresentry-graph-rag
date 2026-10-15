"""완료 Inbox HTTP 경계와 요청·응답 검증."""

from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from app.adapters.refresh_inbox import CONFLICT, NeptuneRefreshInbox, Receipt
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.inbox import request_digest
from app.refresh.models import ArtifactRef, Completion, JobKey, Outcome, RefreshRequest


def request(project_id=None):
    return RefreshRequest(
        JobKey(project_id or str(uuid4()), str(uuid4()), "input-fingerprint"),
        ArtifactRef("input", "refresh.json"),
        (str(uuid4()),),
    )


def completion(req):
    return Completion(req.job, ArtifactRef("result", "refresh.json"), Outcome.NO_CHANGE)


def test_request_digest_includes_inputs_and_settings_not_worker_identity():
    req = request()
    assert request_digest(req) == request_digest(replace(req))
    assert request_digest(req) != request_digest(replace(req, discover_related=False))
    assert request_digest(req) != request_digest(
        replace(req, job=replace(req.job, input_fingerprint="other"))
    )


@pytest.mark.parametrize(
    "data", [[{}], [{"completion_json": []}], [{"completion_json": ["invalid"]}]]
)
def test_inbox_corruption_is_not_an_unprocessed_request(data):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"status": {"code": 200}, "result": {"data": data}}
            )
        )
    ) as client:
        source = NeptuneRefreshInbox("http://127.0.0.1:8182", client=client)
        with pytest.raises(RefreshFailure) as caught:
            source.find(request())
        assert caught.value.failure.code == "REFRESH_INBOX_INVALID"


def test_saved_completion_must_belong_to_the_requested_job():
    req = request()
    encoded = Receipt(
        schema_version="refresh-inbox-v1", completion=completion(request())
    ).model_dump_json()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": {"code": 200},
                    "result": {"data": [{"completion_json": [encoded]}]},
                },
            )
        )
    ) as client:
        with pytest.raises(RefreshFailure) as caught:
            NeptuneRefreshInbox("http://127.0.0.1:8182", client=client).find(req)
    assert caught.value.failure.code == "REFRESH_INBOX_INVALID"


@pytest.mark.parametrize("conflict", [False, True])
def test_neptune_errors_do_not_become_duplicate_success(conflict):
    response = httpx.Response(
        500,
        json={
            "status": {"code": 500, "message": CONFLICT if conflict else "Unavailable"},
        },
    )
    req = request()
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        inbox = NeptuneRefreshInbox("http://127.0.0.1:8182", client=client)
        expected = RequestConflict if conflict else RefreshFailure
        for invoke in (
            lambda: inbox.find(req),
            lambda: inbox.complete(req, completion(req)),
        ):
            with pytest.raises(expected):
                invoke()
