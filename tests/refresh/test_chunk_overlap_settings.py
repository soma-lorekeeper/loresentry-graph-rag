"""Optional sentence overlap preserves source identity and legacy stored bytes."""

import json
from dataclasses import asdict, replace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.adapters.llm_schema import CANDIDATE_VERSION, PROMPT_VERSION, parse_candidate
from app.adapters.refresh_artifacts import S3ArtifactSettings, S3RefreshArtifacts
from app.adapters.refresh_schema import (
    ContextObject,
    RequestObject,
    context_bytes,
    json_bytes,
)
from app.refresh.context import build_context
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.evidence import make_chunks
from app.refresh.models import (
    ArtifactRef,
    DocumentSnapshot,
    ExecutionSnapshot,
    InputSnapshot,
    JobKey,
    ModelInput,
    Outcome,
    RefreshRequest,
    RelatedDocuments,
    SelectionSettings,
    Usage,
)
from app.refresh.rules import RefreshRules


def overlap_snapshot(overlap=8):
    request = RefreshRequest(
        JobKey("project", "request", "fixed-fingerprint"),
        ArtifactRef("input-bucket", "refresh/input.json"),
        ("manuscript",),
        target_ids=("actor",),
        discover_related=False,
        settings=SelectionSettings(chunk_chars=20, chunk_overlap_chars=overlap),
    )
    documents = (
        DocumentSnapshot(
            "project", "manuscript", 1, "앞 문장. 유진이 왔다. 그는 편지를 받았다."
        ),
        DocumentSnapshot(
            "project",
            "actor",
            2,
            "유진은 배달부다.",
            folder_code="CHARACTER",
            title="유진",
        ),
    )
    return ExecutionSnapshot(
        request, documents, RelatedDocuments(), request.target_ids, ("manuscript",)
    )


def declared_candidate(snapshot):
    context = build_context(snapshot, make_chunks(snapshot))
    source = next(
        e
        for e in json.loads(context)["evidence"]
        if e["document_id"] == "manuscript"
        and e["quote"] == "유진이 왔다. 그는 편지를 받았다."
    )
    model_input = ModelInput(
        context,
        CANDIDATE_VERSION,
        PROMPT_VERSION,
        "fake",
        target_ids=snapshot.target_ids,
    )
    payload = {
        "facts": [
            {
                "fact_id": "F1",
                "actor": None,
                "recipient": {"surface": "유진", "document_id": "actor"},
                "object": {"surface": "편지", "document_id": None},
                "action": "받았다",
                "certainty": "CONFIRMED",
                "extent": "UNSPECIFIED",
                "time": "CURRENT",
                "statement": "유진이 편지를 받았다.",
                "source": {"marker": source["marker"]},
            }
        ],
        "document_proposals": [
            {
                "target_document_id": "actor",
                "base_revision_no": 2,
                "field": "body_text",
                "value": "유진은 배달부다. 유진이 편지를 받았다.",
                "evidence": [{"marker": source["marker"]}],
                "fact_ids": ["F1"],
            }
        ],
        "relation_proposals": [],
        "new_document_proposals": [],
    }
    candidate = parse_candidate(
        json.dumps(payload, ensure_ascii=False),
        Usage(1, 20, 30),
        schema_version=CANDIDATE_VERSION,
        model_input=model_input,
    )
    return candidate, model_input


@pytest.mark.parametrize("overlap", [0, 1, 19])
def test_overlap_policy_accepts_only_bounded_integer_settings(overlap):
    snapshot = overlap_snapshot(overlap)
    source = InputSnapshot(snapshot.request.job, (snapshot.documents[0],))
    RefreshRules().validate_input(snapshot.request, source)


@pytest.mark.parametrize("overlap", [-1, 20, 21, True, False, 1.5, "2", None])
def test_overlap_policy_rejects_invalid_values_without_coercion(overlap):
    snapshot = overlap_snapshot(overlap)
    source = InputSnapshot(snapshot.request.job, (snapshot.documents[0],))
    with pytest.raises(RefreshFailure) as caught:
        RefreshRules().validate_input(snapshot.request, source)
    assert caught.value.failure.code == "INVALID_POLICY"


@pytest.mark.parametrize("overlap", [True, False, 1.5, "2", None])
def test_saved_request_schema_rejects_noninteger_overlap(overlap):
    value = RequestObject(
        schema_version="refresh-request-v1", request=overlap_snapshot().request
    ).model_dump(mode="json")
    value["request"]["settings"]["chunk_overlap_chars"] = overlap
    with pytest.raises(ValidationError):
        RequestObject.model_validate_json(json_bytes(value))


def test_default_overlap_preserves_legacy_request_and_context_bytes():
    snapshot = overlap_snapshot(0)
    legacy = {"schema_version": "refresh-context-v1", "snapshot": asdict(snapshot)}
    settings = legacy["snapshot"]["request"]["settings"]
    settings.pop("chunk_overlap_chars")
    settings.pop("separate_discovery")
    legacy["snapshot"]["documents"][0].pop("title")
    body = json_bytes(legacy)
    restored = ContextObject.model_validate_json(body).snapshot
    assert restored == snapshot
    assert restored.request.settings.chunk_overlap_chars == 0
    assert context_bytes(restored) == body

    legacy_request = {
        "schema_version": "refresh-request-v1",
        "request": legacy["snapshot"]["request"],
    }
    request_body = json_bytes(legacy_request)
    store = S3RefreshArtifacts(
        Mock(), S3ArtifactSettings("artifact-bucket", "input-bucket")
    )
    store._put = Mock()
    store._check_request(snapshot.request, create=True)
    assert store._put.call_args.args[2] == request_body
    assert RequestObject.model_validate_json(request_body).request == snapshot.request


def test_positive_overlap_is_stored_and_cannot_change_on_replay():
    snapshot = overlap_snapshot()
    body = context_bytes(snapshot)
    assert (
        json.loads(body)["snapshot"]["request"]["settings"]["chunk_overlap_chars"] == 8
    )
    assert ContextObject.model_validate_json(body).snapshot == snapshot
    store = S3RefreshArtifacts(
        Mock(), S3ArtifactSettings("artifact-bucket", "input-bucket")
    )
    store._read = Mock(return_value=body)
    store._check_request = Mock(return_value=True)
    changed = replace(
        snapshot,
        request=replace(
            snapshot.request,
            settings=replace(snapshot.request.settings, chunk_overlap_chars=0),
        ),
    )
    with pytest.raises(RequestConflict, match="immutable"):
        store.save_context(changed)


def test_overlapping_full_chunk_hydrates_and_passes_exact_server_regeneration():
    snapshot = overlap_snapshot()
    candidate, model_input = declared_candidate(snapshot)
    source = candidate.facts[0].evidence[0]
    manuscript = snapshot.documents[0]
    assert source.quote == manuscript.body_text[source.start : source.end]
    assert source.start == manuscript.body_text.index("유진")
    assert source.revision_no == manuscript.revision_no
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.facts == candidate.facts


def test_valid_original_slice_from_previous_chunk_policy_is_not_an_allowed_source():
    snapshot = overlap_snapshot()
    candidate, model_input = declared_candidate(snapshot)
    old = overlap_snapshot(0)
    source = next(
        chunk.evidence
        for chunk in make_chunks(old)
        if chunk.evidence.quote == "그는 편지를 받았다."
    )
    candidate = replace(
        candidate,
        facts=(replace(candidate.facts[0], evidence=(source,)),),
        document_proposals=(
            replace(candidate.document_proposals[0], evidence=(source,)),
        ),
    )
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.FAILED
    assert result.diagnostics[0].rule == "source.server_chunk"
    assert not result.facts and not result.document_proposals


def test_overlap_repeats_source_text_in_context_for_the_token_budget():
    """겹침으로 반복된 원문도 문맥에 그대로 들어가 호출 직전 토큰 계수에 포함된다."""
    old = overlap_snapshot(0)
    overlapping = overlap_snapshot(8)
    plain = build_context(old, make_chunks(old))
    repeated = build_context(overlapping, make_chunks(overlapping))
    # 반복 원문을 빼거나 잘라 크기를 맞추지 않으므로 겹친 문맥이 더 길다.
    assert len(repeated) > len(plain)
