"""원홉 조회의 응답·준비 상태·실패 경계. 실제 탐색은 Gremlin 통합 테스트로 검증."""

from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from app.adapters.neptune_relations import (
    NeptuneRelatedDocumentSource,
    build_one_hop_query,
)
from app.refresh.errors import RefreshFailure
from app.refresh.models import ArtifactRef, JobKey, Readiness, RefreshRequest


def request():
    return RefreshRequest(
        JobKey(str(uuid4()), str(uuid4()), "input-fingerprint"),
        ArtifactRef("input", "refresh.json"),
        (str(uuid4()),),
    )


def result(req, *, missing=False, rows=None):
    return [
        {
            "seed_ids": [] if missing else [f"file:{req.seed_ids[0]}"],
            "relations": rows or [],
        }
    ]


def lookup(req, data):
    def respond(http_request):
        assert http_request.url.path == "/gremlin"
        assert http_request.method == "POST"
        return httpx.Response(
            200, json={"status": {"code": 200}, "result": {"data": data}}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with NeptuneRelatedDocumentSource(
            "https://writer.neptune.test:8182", client=client
        ) as source:
            return source.find_related(req)


def test_empty_neighborhood_is_ready_but_missing_seed_is_not():
    req = request()
    assert lookup(req, result(req)).readiness == Readiness.READY
    assert lookup(req, result(req, missing=True)).readiness == Readiness.WAITING


def test_one_hop_row_uses_target_category_and_returns_content_uuid():
    req = request()
    target = str(uuid4())
    related = lookup(
        req,
        result(
            req,
            rows=[
                {
                    "seed_id": f"file:{req.seed_ids[0]}",
                    "target_id": f"file:{target}",
                    "relation_id": f"rel:{uuid4()}",
                    "folder_code": ["CHARACTER"],
                    "description": ["연결 설명"],
                }
            ],
        ),
    )
    assert related.document_ids == (target,)
    assert related.observed_version is None
    assert related.relations[0].document_id == req.seed_ids[0]
    assert related.relations[0].target_document_id == target
    assert related.relations[0].relation_key == "related_character"
    assert related.relations[0].description == "연결 설명"


@pytest.mark.parametrize(
    "data", [[], [{}], [{"seed_ids": [], "relations": None}], [None]]
)
def test_bad_graph_response_does_not_become_empty_ready(data):
    with pytest.raises(RefreshFailure) as caught:
        lookup(request(), data)
    assert caught.value.failure.code == "GRAPH_RESPONSE_INVALID"
    assert caught.value.failure.retryable


def test_network_failure_is_retryable_refresh_failure():
    def fail(http_request):
        raise httpx.ReadTimeout("timeout", request=http_request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        source = NeptuneRelatedDocumentSource(
            "https://writer.neptune.test:8182", client=client
        )
        with pytest.raises(RefreshFailure) as caught:
            source.find_related(request())
    assert caught.value.failure.code == "GRAPH_QUERY_FAILED"
    assert caught.value.failure.retryable


def test_required_version_is_unknown_and_zero_depth_or_no_seeds_do_no_io():
    def unexpected(_):
        pytest.fail("No graph query should be made")

    req = request()
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        source = NeptuneRelatedDocumentSource(
            "https://writer.neptune.test:8182", client=client
        )
        assert (
            source.find_related(replace(req, required_graph_version="42")).readiness
            == Readiness.UNKNOWN
        )
        assert (
            source.find_related(replace(req, seed_ids=())).readiness == Readiness.READY
        )
        assert (
            source.find_related(
                replace(req, settings=replace(req.settings, max_depth=0))
            ).readiness
            == Readiness.READY
        )
        with pytest.raises(RefreshFailure) as caught:
            source.find_related(
                replace(req, settings=replace(req.settings, max_depth=2))
            )
        assert caught.value.failure.code == "UNSUPPORTED_GRAPH_DEPTH"


def test_many_neighbors_are_returned_without_row_limit():
    """이웃 수로 자르거나 거절하지 않는다. 조회문에도 limit가 없다."""
    req = request()
    targets = sorted(str(uuid4()) for _ in range(100))
    rows = [
        {
            "seed_id": f"file:{req.seed_ids[0]}",
            "target_id": f"file:{target}",
            "relation_id": f"rel:{uuid4()}",
            "folder_code": ["ITEM"],
            "description": ["연결 설명"],
        }
        for target in targets
    ]
    related = lookup(req, result(req, rows=rows))
    assert related.document_ids == tuple(targets)
    assert len(related.relations) == len(targets)
    assert ".limit(" not in build_one_hop_query(req.job.project_id, req.seed_ids, ())
