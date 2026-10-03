import json
from dataclasses import replace

import pytest

from app.refresh.models import Completion, Failure, Outcome, RefreshResult
from app.refresh.serialization import (
    completion_to_payload,
    result_from_payload,
    result_to_payload,
    storage_failure_completion,
)
from tests.refresh.test_real_service import real_scenario


def test_real_result_json_roundtrip_has_sources_evidence_and_versions():
    s = real_scenario()
    done = s.service.run(s.request, "run1")
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
    done = s.service.run(s.request, "run1")
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
    s.service.run(s.request, "run1")
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


def test_published_result_examples_are_current_and_roundtrip():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "docs/examples/refresh"
    s = real_scenario()
    done = s.service.run(s.request, "run1")
    proposed = json.loads((root / "result-proposed.json").read_text())
    assert proposed == json.loads(s.artifacts.json_results[("project", "request")])
    assert completion_to_payload(done) == json.loads(
        (root / "completed-success.json").read_text()
    )
    for path in root.glob("result-*.json"):
        payload = json.loads(path.read_text())
        assert result_from_payload(payload).outcome.value == payload["outcome"]
