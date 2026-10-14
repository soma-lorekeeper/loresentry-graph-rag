import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.types.responses import Response

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.adapters.llm_schema import CANDIDATE_VERSION, PROMPT_VERSION
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, ModelAttempt, ModelSettings, Outcome, Usage
from app.refresh.rules import RefreshRules
from evaluation.artifacts import AttemptRecorder, RecordingClient
from evaluation.cases import cases
from evaluation.runner import CallBudget, assemble, evaluation_snapshot, run_case


def setup(tmp_path, text):
    case = cases("gpt-5.6-luna", "test")[0]
    case = replace(
        case,
        request=replace(
            case.request,
            model_settings=ModelSettings(
                "gpt-5.6-luna", PROMPT_VERSION, CANDIDATE_VERSION
            ),
        ),
    )
    limits = ModelLimits("gpt-5.6-luna", 10, 80000, 2000, 82000)
    recorder = AttemptRecorder(tmp_path, case, limits, True)
    # Use SDK deserialization for the same candidate shape as a real response.
    from tests.integration.test_openai_http import response_payload

    data = response_payload()
    data["output"][0]["content"][0]["text"] = text
    response = Response.model_validate(data)
    response._request_id = "req_test"
    client = SimpleNamespace(
        max_retries=0, responses=SimpleNamespace(create=Mock(return_value=response))
    )
    model = OpenAIProposalModel(RecordingClient(client, recorder), limits)
    return case, assemble(case, model, CallBudget(3), recorder), recorder


def test_invalid_json_retains_raw_response_usage_and_failure(tmp_path):
    case, scenario, recorder = setup(tmp_path, "{invalid")
    result, _ = run_case(case, scenario)
    assert result.failure.code == "MODEL_INVALID_RESPONSE"
    record = json.loads((tmp_path / "document-attempt-1.json").read_text())
    assert record["raw_response"]["output"][0]["content"][0]["text"] == "{invalid"
    assert record["candidate"] is None
    assert record["failure"]["code"] == "MODEL_INVALID_RESPONSE"
    assert record["usage"]["input_tokens"] == 10
    assert record["usage"]["output_tokens"] == 20
    assert record["usage_known"]
    assert record["provider_request_id"] == "req_test"
    assert record["elapsed_seconds"] >= 0
    assert (tmp_path / "document-attempt-1.json").stat().st_mode & 0o777 == 0o600


def test_storage_failure_keeps_parsed_candidate_and_trace(tmp_path):
    case, scenario, recorder = setup(
        tmp_path,
        '{"facts":[],"document_proposals":[],"relation_proposals":[],"new_document_proposals":[]}',
    )
    scenario.calls.failures["artifacts.save_result"].append(
        RefreshFailure(Failure("STORAGE_TIMEOUT", "timeout", True))
    )
    with pytest.raises(RefreshFailure):
        run_case(case, scenario)
    record = recorder.records[0]
    assert record["candidate"] is not None
    assert record["remote_requested"]
    assert scenario.artifacts.read_result(case.request) is None


def test_preflight_failure_keeps_unknown_usage(tmp_path):
    case, scenario, recorder = setup(tmp_path, "{}")
    case = replace(
        case,
        request=replace(
            case.request,
            model_settings=replace(case.request.model_settings, schema_version="old"),
        ),
    )
    scenario.service.run(case.request, "first")
    record = recorder.records[0]
    assert not record["remote_requested"]
    assert not record["usage_known"]
    assert record["usage"] is None


def test_repair_records_keep_separate_files_and_reconcile_without_duplicate_attempts(
    tmp_path,
):
    case = cases("gpt-5.6-luna", "recording")[0]
    recorder = AttemptRecorder(
        tmp_path, case, ModelLimits("gpt-5.6-luna", 10, 40000, 2000, 42000), False
    )
    snapshot = evaluation_snapshot(case)
    model_input = RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot))[
        0
    ]
    recorder.generate(model_input, lambda: case.expected)
    prompt = json.loads(model_input.prompt)
    prompt["validation_repair"] = {
        "version": "refresh-validation-repair-v1",
        "attempt": 1,
    }
    recorder.generate(
        replace(model_input, prompt=json.dumps(prompt)), lambda: case.expected
    )
    failure = Failure("INVALID_EVIDENCE", "private source detail")
    attempts = (
        ModelAttempt(1, 0, ("c1",), Outcome.FAILED, Usage(1, 10, 20), 1, 0, 0, failure),
        ModelAttempt(1, 1, ("c1",), Outcome.PROPOSED, Usage(1, 12, 22), 1, 0, 0),
    )
    recorder.records[0]["usage"] = {"input_tokens": 10, "output_tokens": 20}
    recorder.records[0]["usage_known"] = True
    recorder.reconcile(attempts)
    recorder.reconcile(attempts)
    paths = sorted(tmp_path.glob("*-attempt-*.json"))
    assert len(paths) == 2
    first, repair = [json.loads(path.read_text()) for path in paths]
    assert first["attempt"] == 1 and repair["attempt"] == 2
    assert first["call_index"] == repair["call_index"] == 1
    assert first["attempt_index"] == 0 and repair["attempt_index"] == 1
    assert repair["repair_version"] == "refresh-validation-repair-v1"
    assert first["validation"]["outcome"] == "FAILED"
    assert repair["validation"]["outcome"] == "PROPOSED"
    assert first["failure"]["code"] == "INVALID_EVIDENCE"
    assert first["usage"] == {"input_tokens": 10, "output_tokens": 20}
    assert first["usage_known"]
    assert first["candidate"] is not None and repair["candidate"] is not None


def test_exhausted_budget_does_not_create_an_attempt_record(tmp_path):
    case = cases("gpt-5.6-luna", "budget")[0]
    recorder = AttemptRecorder(
        tmp_path, case, ModelLimits("gpt-5.6-luna", 10, 40000, 2000, 42000), False
    )
    budget = CallBudget(1)
    budget.consume()
    delegate = Mock()
    scenario = assemble(case, delegate, budget, recorder)
    snapshot = evaluation_snapshot(case)
    model_input = RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot))[
        0
    ]
    with pytest.raises(RefreshFailure) as error:
        scenario.service.model.generate(model_input)
    assert error.value.failure.code == "EVALUATION_CALL_BUDGET"
    assert not recorder.records
    assert not list(tmp_path.glob("*.json"))
    assert budget.used == 1
    delegate.generate.assert_not_called()
