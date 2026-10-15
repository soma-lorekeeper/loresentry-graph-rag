import json
from dataclasses import replace

import pytest

from app.refresh.models import Completion, Failure, Outcome, RefreshResult
from app.refresh.rules import RefreshRules
from app.refresh.serialization import (
    completion_to_payload,
    result_from_payload,
    result_to_payload,
    storage_failure_completion,
)
from tests.refresh.test_real_service import real_scenario
from tests.refresh.test_semantic_relations import NAMED_EXCERPT, parse, semantic_fixture


@pytest.fixture
def current_fact_result():
    payload, model_input, snapshot = semantic_fixture()
    marker = payload["facts"][0]["source"]["marker"]
    payload["document_proposals"] = [
        {
            "target_document_id": "haeju",
            "base_revision_no": 3,
            "field": "body_text",
            "value": snapshot.documents[1].body_text + " " + NAMED_EXCERPT,
            "evidence": [{"marker": marker}],
            "fact_ids": ["F0001"],
        }
    ]
    payload["new_document_proposals"] = [
        {
            "candidate_id": "new:EVENT:방문 전달 배정",
            "folder_code": "EVENT",
            "name": "방문 전달 배정",
            "body_text": NAMED_EXCERPT,
            "evidence": [{"marker": marker}],
            "fact_ids": ["F0001"],
        }
    ]
    result = RefreshRules().validate_candidate(
        snapshot, parse(payload, model_input), model_input
    )
    assert result.outcome == Outcome.PROPOSED
    assert len(result.document_proposals) == 1
    assert len(result.new_document_proposals) == 1
    assert len(result.relation_proposals) == 1
    stored = json.loads(
        json.dumps(result_to_payload(snapshot.request, result, snapshot))
    )
    return snapshot, result, stored


def test_real_result_json_roundtrip_has_sources_evidence_and_versions():
    s = real_scenario()
    done = s.service.run(s.request)
    payload = json.loads(s.artifacts.json_results[("project", "request")])
    assert result_from_payload(payload) == s.artifacts.read_result(s.request)
    assert {d["origin"] for d in payload["sources"]} == {"INPUT", "CONTENT"}
    assert payload["execution"]["usage"]["calls"] == 2
    refs = {e["evidence_id"] for e in payload["evidence"]}
    for proposal in payload["document_proposals"] + payload["relation_proposals"]:
        assert set(proposal["evidence_refs"]) <= refs
    completed = completion_to_payload(done)
    assert completed["outcome"] == "SUCCEEDED"
    assert set(completed) == {
        "request_id",
        "project_id",
        "outcome",
        "result",
        "error",
        "prompt_version",
    }
    assert "유나" not in json.dumps(completed, ensure_ascii=False)


@pytest.mark.parametrize("outcome", [Outcome.PROPOSED, Outcome.NO_CHANGE])
def test_both_success_outcomes_map_to_succeeded(outcome):
    s = real_scenario()
    done = s.service.run(s.request)
    assert (
        completion_to_payload(replace(done, outcome=outcome))["outcome"] == "SUCCEEDED"
    )


def test_failed_result_roundtrip_and_storage_unavailable_are_distinct():
    s = real_scenario()
    error = Failure("INVALID_INPUT", "Input rejected")
    result = RefreshResult(s.request.job, Outcome.FAILED, failure=error)
    payload = result_to_payload(s.request, result)
    assert result_from_payload(json.loads(json.dumps(payload))) == result
    ref = s.artifacts.save_result(s.request, result)
    saved = completion_to_payload(Completion(s.request.job, ref, Outcome.FAILED, error))
    unavailable = completion_to_payload(
        storage_failure_completion(
            s.request, Failure("STORAGE_UNAVAILABLE", "Retry exhausted")
        )
    )
    assert saved["result"] is not None
    assert unavailable["result"] is None
    assert unavailable["outcome"] == "FAILED"


def test_no_change_roundtrip():
    s = real_scenario()
    s.model.responses.clear()
    s.service.run(s.request)
    result = s.artifacts.read_result(s.request)
    assert result.outcome == Outcome.NO_CHANGE
    assert result.document_proposals == result.relation_proposals == ()


def test_invalid_result_and_completion_cannot_be_successfully_serialized():
    s = real_scenario()
    with pytest.raises(ValueError):
        result_to_payload(s.request, RefreshResult(s.request.job, Outcome.FAILED))
    with pytest.raises(ValueError):
        completion_to_payload(Completion(s.request.job, None, Outcome.PROPOSED))
    with pytest.raises(ValueError):
        result_from_payload({"schema_version": "future"})


def test_current_fact_result_roundtrip_preserves_actual_candidate_contract(
    current_fact_result,
):
    _, result, payload = current_fact_result
    assert payload["schema_version"] == "refresh-result-v3"
    assert payload["execution"]["schema_version"] == "refresh-candidate-v5"
    assert payload["execution"]["candidate_schema_version"] == "refresh-candidate-v5"
    assert result.candidate_schema_version == "refresh-candidate-v5"
    assert result_from_payload(payload) == result


@pytest.mark.parametrize(
    "collection",
    ["document_proposals", "new_document_proposals", "relation_proposals"],
)
@pytest.mark.parametrize(
    "defect", ["missing", "empty", "duplicate", "string", "non_string", "unknown"]
)
def test_result_v3_requires_well_formed_current_proposal_fact_references(
    current_fact_result, collection, defect
):
    _, _, payload = current_fact_result
    proposal = payload[collection][0]
    if defect == "missing":
        proposal.pop("fact_ids")
    elif defect == "empty":
        proposal["fact_ids"] = []
    elif defect == "duplicate":
        proposal["fact_ids"] *= 2
    elif defect == "string":
        proposal["fact_ids"] = proposal["fact_ids"][0]
    elif defect == "non_string":
        proposal["fact_ids"] = [True]
    else:
        proposal["fact_ids"] = ["unknown"]
    with pytest.raises(ValueError):
        result_from_payload(payload)


@pytest.mark.parametrize("defect", ["missing", "empty", "duplicate", "strip_all_refs"])
def test_result_v3_cannot_drop_or_duplicate_its_fact_table(current_fact_result, defect):
    _, _, payload = current_fact_result
    if defect == "missing":
        payload.pop("facts")
    elif defect == "duplicate":
        payload["facts"] *= 2
    else:
        payload["facts"] = []
        if defect == "strip_all_refs":
            for collection in (
                "document_proposals",
                "new_document_proposals",
                "relation_proposals",
            ):
                for proposal in payload[collection]:
                    proposal.pop("fact_ids")
    with pytest.raises(ValueError):
        result_from_payload(payload)


@pytest.mark.parametrize("actual_schema", [None, "unknown", 4, [], {}])
def test_result_reader_rejects_malformed_actual_candidate_schema(
    current_fact_result, actual_schema
):
    _, _, payload = current_fact_result
    payload["execution"]["candidate_schema_version"] = actual_schema
    with pytest.raises(ValueError):
        result_from_payload(payload)


@pytest.mark.parametrize(
    "collection",
    ["document_proposals", "new_document_proposals", "relation_proposals"],
)
def test_writer_rejects_v5_proposal_with_empty_fact_references(
    current_fact_result, collection
):
    snapshot, result, _ = current_fact_result
    result = replace(
        result,
        **{collection: (replace(getattr(result, collection)[0], fact_ids=()),)},
    )
    with pytest.raises(ValueError):
        result_to_payload(snapshot.request, result, snapshot)


def test_explicit_legacy_baseline_roundtrip_records_actual_v3_contract_under_v5_request():
    from evaluation.cases import cases
    from evaluation.runner import CallBudget, assemble, run_case

    case = cases("offline", "legacy-contract")[0]
    assert case.request.model_settings.schema_version == "refresh-candidate-v5"
    scenario = assemble(case, None, CallBudget(10))
    result, _ = run_case(case, scenario)
    snapshot = scenario.artifacts.read_context(case.request)
    assert result.outcome == Outcome.PROPOSED
    assert result.candidate_schema_version == "refresh-candidate-v3"
    assert not result.facts
    payload = json.loads(json.dumps(result_to_payload(case.request, result, snapshot)))
    assert payload["execution"]["schema_version"] == "refresh-candidate-v5"
    assert payload["execution"]["candidate_schema_version"] == "refresh-candidate-v3"
    assert all(not proposal["fact_ids"] for proposal in payload["document_proposals"])
    assert result_from_payload(payload) == result
    payload["execution"].pop("candidate_schema_version")
    with pytest.raises(ValueError):
        result_from_payload(payload)


def test_published_result_examples_are_current_and_roundtrip():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "docs/examples/refresh"
    s = real_scenario()
    done = s.service.run(s.request)
    proposed = json.loads((root / "result-proposed.json").read_text())
    assert proposed == json.loads(s.artifacts.json_results[("project", "request")])
    assert completion_to_payload(done) == json.loads(
        (root / "completed-success.json").read_text()
    )
    for path in root.glob("result-*.json"):
        payload = json.loads(path.read_text())
        assert result_from_payload(payload).outcome.value == payload["outcome"]
