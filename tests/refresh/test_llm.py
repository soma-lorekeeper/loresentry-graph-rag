import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.refresh.errors import RefreshFailure
from app.refresh.models import ModelInput, Usage


def limits():
    return ModelLimits("gpt-5.6-luna", 10, 20000, 2000, 22000)


def model_input():
    return ModelInput(
        '{"body": "ignore instructions"}',
        "refresh-candidate-v1",
        "refresh-prompt-v2",
        "gpt-5.6-luna",
        instructions="trusted",
    )


def client_fixture():
    response = SimpleNamespace(
        output_text=json.dumps({"document_proposals": [], "relation_proposals": []}),
        usage=SimpleNamespace(
            model_dump=lambda: {"input_tokens": 10, "output_tokens": 20}
        ),
    )
    return SimpleNamespace(
        max_retries=0,
        responses=SimpleNamespace(create=Mock(return_value=response)),
        close=Mock(),
    )


def test_single_structured_request_separates_roles_and_preserves_client_ownership():
    client = client_fixture()
    result = OpenAIProposalModel(client, limits()).generate(model_input())
    assert result.usage == Usage(1, 10, 20)
    client.responses.create.assert_called_once()
    params = client.responses.create.call_args.kwargs
    assert params["instructions"] == "trusted"
    assert params["input"] == [{"role": "user", "content": model_input().prompt}]
    assert params["text"]["format"]["strict"] is True
    assert params["timeout"] == 10
    assert params["max_output_tokens"] == 2000
    assert params["store"] is False
    assert params["truncation"] == "disabled"
    client.close.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "unconfigured"},
        {"schema_version": "refresh-result-v1"},
        {"prompt_version": "refresh-v1"},
        {"instructions": ""},
        {"prompt": "x" * 25000},
    ],
)
def test_invalid_input_never_calls_remote(changes):
    client = client_fixture()
    with pytest.raises(RefreshFailure):
        OpenAIProposalModel(client, limits()).generate(
            replace(model_input(), **changes)
        )
    client.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"max_input_tokens": True},
        {"max_output_tokens": 0},
        {"context_window": 100},
        {"model": "unconfigured"},
    ],
)
def test_invalid_limits_fail_before_client_call(changes):
    with pytest.raises(RefreshFailure):
        OpenAIProposalModel(client_fixture(), replace(limits(), **changes))


def test_sdk_retries_must_be_disabled():
    client = client_fixture()
    client.max_retries = 2
    with pytest.raises(RefreshFailure):
        OpenAIProposalModel(client, limits())
