"""실제 Content HTTP 전송을 refresh에 연결한다. 로컬 API 대체 서버를 사용한다."""

from uuid import uuid4

import pytest

from app.adapters.content_documents import ContentDocumentSource
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    ArtifactRef,
    DocumentSnapshot,
    Failure,
    InputSnapshot,
    JobKey,
    ModelCandidate,
    ModelSettings,
    Outcome,
    RefreshRequest,
    RelatedDocuments,
    Relation,
)
from app.refresh.service import RefreshService
from evaluation.fakes import (
    Calls,
    FakeInbox,
    FakeJsonArtifacts,
    FakeModel,
    FakePublisher,
    FakeRelations,
)
from tests.fakes.content import content_payload

pytestmark = pytest.mark.integration


def setup_refresh(content_server):
    state, endpoint = content_server
    user, project, seed, neighbor = (str(uuid4()) for _ in range(4))
    req = RefreshRequest(
        JobKey(project, str(uuid4()), "immutable-input"),
        ArtifactRef("inputs", "refresh.json"),
        (seed,),
        model_settings=ModelSettings(
            "fake", "refresh-prompt-v7", "refresh-candidate-v3"
        ),
    )
    original = DocumentSnapshot(project, seed, 0, "S3 입력 원고")
    calls = Calls()
    artifacts = FakeJsonArtifacts(
        {req.input_ref: InputSnapshot(req.job, (original,))}, calls
    )
    model = FakeModel(ModelCandidate(()), calls)
    state["responses"][f"/projects/{project}"] = (
        200,
        {"id": project, "trashed_at": None},
    )
    state["responses"][f"/files/{neighbor}/content"] = (
        200,
        content_payload(project, neighbor, revision_no=0),
    )
    related = RelatedDocuments(
        (neighbor,),
        (Relation(project, seed, neighbor, "related_character"),),
    )
    return (
        state,
        endpoint,
        user,
        req,
        original,
        neighbor,
        calls,
        artifacts,
        model,
        related,
    )


def service(adapter, calls, artifacts, model, related):
    return RefreshService(
        artifacts=artifacts,
        relations=FakeRelations(related, calls),
        documents=adapter,
        model=model,
        inbox=FakeInbox(calls=calls),
        publisher=FakePublisher(calls),
    )


def test_real_http_fetch_and_frozen_context_after_model_failure(content_server):
    state, endpoint, user, req, original, neighbor, calls, artifacts, model, related = (
        setup_refresh(content_server)
    )
    model.responses.append(
        RefreshFailure(Failure("MODEL_UNAVAILABLE", "Retry model", True))
    )
    with ContentDocumentSource(endpoint, user_id=user) as adapter:
        refresh = service(adapter, calls, artifacts, model, related)
        with pytest.raises(RefreshFailure, match="Retry model"):
            refresh.run(req)
        frozen = artifacts.read_context(req)
        assert (
            next(
                doc
                for doc in frozen.documents
                if doc.document_id == original.document_id
            )
            == original
        )
        fetched = next(doc for doc in frozen.documents if doc.document_id == neighbor)
        assert (fetched.body_text, fetched.revision_no) == ("Content 원문", 0)
        assert fetched.properties == (("aliases", "별칭  "),)
        assert len(state["requests"]) == 2
        assert all(headers["X-User-Id"] == user for _, headers in state["requests"])
        state["responses"].clear()
        completion = refresh.run(req)
        assert completion.outcome == Outcome.NO_CHANGE
        assert artifacts.read_context(req) == frozen
        assert len(state["requests"]) == 2
    assert adapter._client.is_closed


@pytest.mark.parametrize(
    "status,payload,code,retryable",
    [
        (404, {"code": "FILE_NOT_FOUND"}, "DOCUMENT_MISSING", False),
        (403, {"code": "FORBIDDEN"}, "CONTENT_ACCESS_DENIED", False),
        (503, {"code": "INTERNAL_ERROR"}, "CONTENT_UNAVAILABLE", True),
        (200, {"id": "wrong-body"}, "CONTENT_RESPONSE_INVALID", False),
    ],
)
def test_content_failure_stops_generation(
    content_server, status, payload, code, retryable
):
    state, endpoint, user, req, _, neighbor, calls, artifacts, model, related = (
        setup_refresh(content_server)
    )
    state["responses"][f"/files/{neighbor}/content"] = (status, payload)
    with ContentDocumentSource(endpoint, user_id=user) as adapter:
        refresh = service(adapter, calls, artifacts, model, related)
        if retryable:
            with pytest.raises(RefreshFailure) as caught:
                refresh.run(req)
            assert caught.value.failure.code == code
        else:
            completion = refresh.run(req)
            assert completion.outcome == Outcome.FAILED
            assert artifacts.read_result(req).failure.code == code
    assert "model.generate" not in calls.names()
    assert artifacts.read_context(req) is None


def test_project_rejection_blocks_all_body_requests(content_server):
    state, endpoint, user, req, _, _, calls, artifacts, model, related = setup_refresh(
        content_server
    )
    state["responses"][f"/projects/{req.job.project_id}"] = (
        404,
        {"code": "PROJECT_NOT_FOUND"},
    )
    with ContentDocumentSource(endpoint, user_id=user) as adapter:
        completion = service(adapter, calls, artifacts, model, related).run(req)
    assert completion.outcome == Outcome.FAILED
    assert artifacts.read_result(req).failure.code == "CONTENT_PROJECT_UNAVAILABLE"
    assert [path for path, _ in state["requests"]] == [
        f"/projects/{req.job.project_id}"
    ]
    assert "model.generate" not in calls.names()
