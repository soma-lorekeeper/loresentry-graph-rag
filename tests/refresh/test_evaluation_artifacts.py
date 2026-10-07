import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai.types.responses import Response

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure
from evaluation.artifacts import AttemptRecorder, RecordingClient
from evaluation.cases import cases
from evaluation.runner import CallBudget, assemble, run_case


def setup(tmp_path, text):
    case = cases("gpt-5.6-luna", "test")[0]
    limits = ModelLimits("gpt-5.6-luna", 10, 20000, 2000, 22000)
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
    return case, assemble(case, model, CallBudget(2), recorder), recorder


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
        '{"document_proposals":[],"relation_proposals":[],"new_document_proposals":[]}',
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
