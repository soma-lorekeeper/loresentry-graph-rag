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
        status="completed",
        output=[],
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


@pytest.mark.parametrize(
    "status,output,text,code",
    [
        ("incomplete", [], "{}", "MODEL_INCOMPLETE"),
        (
            "completed",
            [
                SimpleNamespace(
                    type="message", content=[SimpleNamespace(type="refusal")]
                )
            ],
            "",
            "MODEL_REFUSED",
        ),
        ("completed", [], "", "MODEL_EMPTY_RESPONSE"),
        ("completed", [], "{", "MODEL_INVALID_RESPONSE"),
    ],
)
def test_non_candidates_never_become_no_change(status, output, text, code):
    client = client_fixture()
    response = client.responses.create.return_value
    response.status, response.output, response.output_text = status, output, text
    with pytest.raises(RefreshFailure) as caught:
        OpenAIProposalModel(client, limits()).generate(model_input())
    assert caught.value.failure.code == code
    assert not caught.value.failure.retryable


@pytest.mark.parametrize(
    "status,body,code,retryable",
    [
        (400, {}, "MODEL_REQUEST_INVALID", False),
        (401, {}, "MODEL_AUTH_FAILED", False),
        (403, {}, "MODEL_AUTH_FAILED", False),
        (429, {}, "MODEL_RATE_LIMITED", True),
        (429, {"code": "insufficient_quota"}, "MODEL_QUOTA_EXHAUSTED", False),
        (
            429,
            {"error": {"code": "billing_hard_limit_reached"}},
            "MODEL_QUOTA_EXHAUSTED",
            False,
        ),
        (500, {}, "MODEL_UNAVAILABLE", True),
    ],
)
def test_status_classification_is_safe_and_never_retries(status, body, code, retryable):
    import httpx2
    from openai import APIStatusError

    client = client_fixture()
    response = httpx2.Response(status, request=httpx2.Request("POST", "http://test"))
    client.responses.create.side_effect = APIStatusError(
        "SECRET DOCUMENT AND KEY", response=response, body=body
    )
    with pytest.raises(RefreshFailure) as caught:
        OpenAIProposalModel(client, limits()).generate(model_input())
    assert caught.value.failure.code == code
    assert caught.value.failure.retryable == retryable
    assert "SECRET" not in str(caught.value)
    client.responses.create.assert_called_once()


def test_timeout_connection_and_programming_error_are_distinct():
    import httpx2
    from openai import APIConnectionError, APITimeoutError

    request = httpx2.Request("POST", "http://test")
    for error, code in [
        (APITimeoutError(request=request), "MODEL_TIMEOUT"),
        (APIConnectionError(request=request), "MODEL_CONNECTION_FAILED"),
    ]:
        client = client_fixture()
        client.responses.create.side_effect = error
        with pytest.raises(RefreshFailure) as caught:
            OpenAIProposalModel(client, limits()).generate(model_input())
        assert caught.value.failure.code == code
        assert caught.value.failure.retryable
        client.responses.create.assert_called_once()
    client.responses.create.side_effect = TypeError("programming defect")
    with pytest.raises(TypeError):
        OpenAIProposalModel(client, limits()).generate(model_input())


def test_adapter_with_real_service_keeps_all_or_nothing_and_saved_recovery():
    from app.refresh.models import Outcome
    from tests.refresh.test_real_service import real_scenario

    s = real_scenario()
    s.request = replace(
        s.request,
        model_settings=replace(s.request.model_settings, model="gpt-5.6-luna"),
    )
    client = client_fixture()
    good = client.responses.create.return_value
    bad = SimpleNamespace(status="incomplete", output=[], output_text="")
    client.responses.create.side_effect = [good, bad]
    s.service.model = OpenAIProposalModel(client, limits())
    done = s.service.run(s.request, "first")
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == "MODEL_INCOMPLETE"
    result = s.artifacts.read_result(s.request)
    assert result.document_proposals == result.relation_proposals == ()
    assert s.service.run(s.request, "repeat") == done
    assert client.responses.create.call_count == 2
