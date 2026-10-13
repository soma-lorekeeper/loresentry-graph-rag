"""저장된 보완 이력의 복원과 사용량·순서 손상 거절."""

import json
from dataclasses import replace

import pytest

from app.refresh.models import (
    Failure,
    ModelAttempt,
    Outcome,
    Usage,
    ValidationDiagnostic,
)
from app.refresh.serialization import result_from_payload, result_to_payload
from tests.fakes.fixtures import example


@pytest.fixture
def repair_result():
    request, _, _, snapshot, _, baseline = example()
    diagnostic = ValidationDiagnostic(
        "facts[0].action", "fact.action_in_source", "F1", "C0001", "바뀐 표현"
    )
    attempts = (
        ModelAttempt(
            1,
            0,
            ("setting-1",),
            Outcome.FAILED,
            Usage(1, 5, 6),
            document_proposals=1,
            relation_proposals=0,
            new_document_proposals=0,
            failure=Failure("INVALID_FACT", "Original action required"),
            diagnostics=(diagnostic,),
        ),
        ModelAttempt(
            1,
            1,
            ("setting-1",),
            Outcome.NO_CHANGE,
            Usage(1, 6, 7),
            document_proposals=0,
            relation_proposals=0,
            new_document_proposals=0,
        ),
    )
    result = replace(
        baseline,
        outcome=Outcome.NO_CHANGE,
        document_proposals=(),
        usage=Usage(2, 11, 13),
        attempts=attempts,
    )
    return request, snapshot, result


def test_attempt_history_roundtrip_retains_failed_usage_and_diagnostics(repair_result):
    request, snapshot, result = repair_result
    payload = result_to_payload(request, result, snapshot)
    assert payload["execution"]["attempts"][0]["diagnostics"][0]["source_marker"] == (
        "C0001"
    )
    restored = result_from_payload(json.loads(json.dumps(payload)))
    assert restored == result


def test_aggregate_failure_diagnostics_roundtrip_without_extra_model_call(
    repair_result,
):
    request, snapshot, result = repair_result
    result = replace(
        result,
        outcome=Outcome.FAILED,
        failure=Failure("CONFLICTING_PROPOSALS", "Aggregate conflict"),
        diagnostics=(ValidationDiagnostic("document_proposals", "aggregate.conflict"),),
    )
    assert result_from_payload(result_to_payload(request, result, snapshot)) == result


@pytest.mark.parametrize(
    "mutation",
    [
        "usage",
        "order",
        "repair_index",
        "boolean_count",
        "outcome",
        "diagnostic_type",
        "different_targets",
        "success_then_repair",
        "failed_final_attempt",
    ],
)
def test_corrupted_attempt_history_is_rejected(repair_result, mutation):
    request, snapshot, result = repair_result
    payload = result_to_payload(request, result, snapshot)
    attempts = payload["execution"]["attempts"]
    if mutation == "usage":
        attempts[0]["usage"]["input_tokens"] += 1
    elif mutation == "order":
        attempts[0]["call_index"] = 2
    elif mutation == "repair_index":
        attempts[1]["attempt_index"] = 2
    elif mutation == "boolean_count":
        attempts[0]["document_proposals"] = True
    elif mutation == "outcome":
        attempts[0]["outcome"] = "UNKNOWN"
    elif mutation == "diagnostic_type":
        attempts[0]["diagnostics"][0]["source_marker"] = 42
    elif mutation == "different_targets":
        attempts[1]["target_ids"] = ["other"]
    elif mutation == "success_then_repair":
        attempts[0]["outcome"] = "NO_CHANGE"
        attempts[0]["failure"] = None
    else:
        attempts[1]["outcome"] = "FAILED"
        attempts[1]["failure"] = {
            "code": "INVALID_FACT",
            "message": "invalid",
            "retryable": False,
        }
    with pytest.raises(ValueError):
        result_from_payload(payload)


def test_historical_result_without_attempt_metadata_still_reads(repair_result):
    request, snapshot, result = repair_result
    result = replace(result, attempts=())
    payload = result_to_payload(request, result, snapshot)
    assert "attempts" not in payload["execution"]
    assert result_from_payload(payload) == result
