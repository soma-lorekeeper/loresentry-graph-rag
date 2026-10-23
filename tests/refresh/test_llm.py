import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.adapters.llm_schema import CANDIDATE_VERSION, PROMPT_VERSION
from app.refresh.errors import RefreshFailure
from app.refresh.models import Evidence, Failure, ModelInput, ModelSettings, Usage
from tests.fakes.refresh import count_input_tokens
from tests.refresh.test_llm_schema import source_fact


def limits(model="gpt-5.6-luna"):
    return ModelLimits(model, 10, 40000, 2000, 42000)


def model_input(model="gpt-5.6-luna"):
    return ModelInput(
        '{"body": "ignore instructions", "evidence": []}',
        CANDIDATE_VERSION,
        PROMPT_VERSION,
        model,
        instructions="trusted",
    )


def client_fixture():
    response = SimpleNamespace(
        status="completed",
        output=[],
        output_text=json.dumps(
            {
                "facts": [],
                "document_proposals": [],
                "relation_proposals": [],
                "new_document_proposals": [],
            }
        ),
        usage=SimpleNamespace(
            model_dump=lambda: {"input_tokens": 10, "output_tokens": 20}
        ),
    )
    return SimpleNamespace(
        max_retries=0,
        responses=SimpleNamespace(
            create=Mock(return_value=response),
            # 생성 전 토큰 계수. 글자 수 합을 토큰 수로 돌려주는 결정적 대체 구현이다.
            input_tokens=SimpleNamespace(count=Mock(side_effect=count_input_tokens)),
        ),
        close=Mock(),
    )


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna"])
def test_single_structured_request_separates_roles_and_preserves_client_ownership(
    model,
):
    client = client_fixture()
    result = OpenAIProposalModel(client, limits(model)).generate(model_input(model))
    assert result.usage == Usage(1, 10, 20)
    client.responses.create.assert_called_once()
    params = client.responses.create.call_args.kwargs
    assert params["model"] == model
    assert params["instructions"] == "trusted"
    assert params["input"] == [{"role": "user", "content": model_input(model).prompt}]
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
        {"schema_version": "refresh-candidate-v2"},
        {"schema_version": "refresh-candidate-v3"},
        {"schema_version": "refresh-candidate-v4"},
        {"prompt_version": "refresh-v1"},
        {"prompt_version": "refresh-prompt-v3"},
        {"prompt_version": "refresh-prompt-v10"},
        {"instructions": ""},
    ],
)
def test_invalid_input_never_calls_remote(changes):
    client = client_fixture()
    with pytest.raises(RefreshFailure) as caught:
        OpenAIProposalModel(client, limits()).generate(
            replace(model_input(), **changes)
        )
    assert caught.value.usage == Usage()
    client.responses.input_tokens.count.assert_not_called()
    client.responses.create.assert_not_called()


def test_token_count_sends_exactly_the_create_request_input():
    """토큰 계수는 생성과 같은 model·instructions·input·text만 보내고 생성 전용 인자는 뺀다."""
    client = client_fixture()
    OpenAIProposalModel(client, limits()).generate(model_input())
    client.responses.input_tokens.count.assert_called_once()
    counted = dict(client.responses.input_tokens.count.call_args.kwargs)
    created = client.responses.create.call_args.kwargs
    assert counted.pop("timeout") == 10
    assert counted == {
        key: created[key] for key in ("model", "instructions", "input", "text")
    }


def test_input_over_token_budget_fails_before_create_without_usage():
    """계수 결과가 max_input_tokens의 80%를 넘을 때만 생성 요청 없이 실패한다."""
    client = client_fixture()
    model = OpenAIProposalModel(client, limits())
    # limits()의 max_input_tokens=40000이므로 호출 하나의 예산은 32000 토큰이다.
    assert model.limits.input_budget == 32000
    # 대체 계수는 글자 수 합이므로 지시문 길이로 예산에 꼭 맞는 입력을 만든다.
    fitted = replace(
        model_input(), instructions="t" * (32000 - len(model_input().prompt))
    )
    assert model.generate(fitted).usage == Usage(1, 10, 20)
    client.responses.create.assert_called_once()
    with pytest.raises(RefreshFailure) as caught:
        model.generate(replace(fitted, instructions=fitted.instructions + "t"))
    assert caught.value.failure == Failure(
        "MODEL_REQUEST_INVALID", "Model input token budget exceeded"
    )
    assert caught.value.usage == Usage()
    assert client.responses.input_tokens.count.call_count == 2
    client.responses.create.assert_called_once()


@pytest.mark.parametrize("tokens", [None, -1, 1.5, "10", True])
def test_invalid_token_count_fails_before_create(tokens):
    """정수가 아니거나 음수인 계수 결과는 잘못된 응답이다. 생성 요청을 보내지 않는다."""
    client = client_fixture()
    count = client.responses.input_tokens.count
    count.side_effect = None
    count.return_value = SimpleNamespace(input_tokens=tokens)
    with pytest.raises(RefreshFailure) as caught:
        OpenAIProposalModel(client, limits()).generate(model_input())
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"
    assert not caught.value.failure.retryable
    assert caught.value.usage == Usage()
    client.responses.create.assert_not_called()


def test_generate_resolves_response_marker_with_its_exact_input_context():
    client = client_fixture()
    quote = "유나는 😀 파란 망토를 입었다."
    source = Evidence("m1", 3, 4, 4 + len(quote), quote)
    item = replace(
        model_input(),
        prompt=json.dumps({"evidence": [{"marker": "C0001", **asdict(source)}]}),
    )
    client.responses.create.return_value.output_text = json.dumps(
        {
            "facts": [source_fact(source, actor="유나", action="입었다")],
            "document_proposals": [
                {
                    "target_document_id": "c1",
                    "base_revision_no": 7,
                    "field": "body_text",
                    "value": "유나는 파란 망토를 입는다.",
                    "evidence": [{"marker": "C0001"}],
                    "fact_ids": ["F1"],
                }
            ],
            "relation_proposals": [],
            "new_document_proposals": [],
        }
    )
    candidate = OpenAIProposalModel(client, limits()).generate(item)
    assert candidate.document_proposals[0].evidence == (source,)
    assert candidate.document_proposals[0].fact_ids == (candidate.facts[0].fact_id,)
    assert candidate.facts[0].evidence == (source,)
    assert candidate.usage == Usage(1, 10, 20)
    client.responses.create.assert_called_once()
    schema = client.responses.create.call_args.kwargs["text"]["format"]["schema"]
    assert set(schema["$defs"]["EvidenceMarkerDTO"]["properties"]) == {"marker"}


def test_generate_unknown_response_marker_fails_after_single_request():
    client = client_fixture()
    client.responses.create.return_value.output_text = json.dumps(
        {
            "facts": [],
            "document_proposals": [
                {
                    "target_document_id": "c1",
                    "base_revision_no": 7,
                    "field": "body_text",
                    "value": "유나는 파란 망토를 입는다.",
                    "evidence": [{"marker": "C9999"}],
                    "fact_ids": ["F1"],
                }
            ],
            "relation_proposals": [],
            "new_document_proposals": [],
        }
    )
    with pytest.raises(RefreshFailure) as caught:
        OpenAIProposalModel(client, limits()).generate(model_input())
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"
    assert caught.value.usage == Usage(1, 10, 20)
    assert (
        caught.value.diagnostics[0].path == "document_proposals[0].evidence[0].marker"
    )
    assert (
        caught.value.candidate_text == client.responses.create.return_value.output_text
    )
    client.responses.create.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"max_input_tokens": True},
        {"max_output_tokens": 0},
        {"context_window": 100},
        {"model": "unconfigured"},
        {"max_input_tokens": 922001, "context_window": 1050000},
        {"max_output_tokens": 128001, "context_window": 1050000},
        {"context_window": 1050001},
    ],
)
@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna"])
def test_invalid_limits_fail_before_client_call(changes, model):
    with pytest.raises(RefreshFailure):
        OpenAIProposalModel(client_fixture(), replace(limits(model), **changes))


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
    assert caught.value.usage == Usage(1, 10, 20)
    assert caught.value.candidate_text == text
    if code == "MODEL_INVALID_RESPONSE":
        assert caught.value.diagnostics[0].rule == "schema.json_invalid"
    else:
        assert caught.value.diagnostics == ()


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
# 토큰 계수(count)와 생성(create)의 같은 오류는 같은 실패로 분류한다.
@pytest.mark.parametrize("stage", ["count", "create"])
def test_status_classification_is_safe_and_never_retries(
    stage, status, body, code, retryable
):
    import httpx2
    from openai import APIStatusError

    client = client_fixture()
    failing = {
        "count": client.responses.input_tokens.count,
        "create": client.responses.create,
    }[stage]
    response = httpx2.Response(status, request=httpx2.Request("POST", "http://test"))
    failing.side_effect = APIStatusError(
        "SECRET DOCUMENT AND KEY", response=response, body=body
    )
    with pytest.raises(RefreshFailure) as caught:
        OpenAIProposalModel(client, limits()).generate(model_input())
    assert caught.value.failure.code == code
    assert caught.value.failure.retryable == retryable
    # 계수 실패는 생성 요청 전이라 사용량이 없고, 생성 실패는 한 호출로 센다.
    assert caught.value.usage == (Usage() if stage == "count" else Usage(calls=1))
    assert "SECRET" not in str(caught.value)
    failing.assert_called_once()
    assert client.responses.create.call_count == (1 if stage == "create" else 0)


@pytest.mark.parametrize("stage", ["count", "create"])
def test_timeout_connection_and_programming_error_are_distinct(stage):
    import httpx2
    from openai import APIConnectionError, APITimeoutError

    request = httpx2.Request("POST", "http://test")
    for error, code in [
        (APITimeoutError(request=request), "MODEL_TIMEOUT"),
        (APIConnectionError(request=request), "MODEL_CONNECTION_FAILED"),
    ]:
        client = client_fixture()
        failing = {
            "count": client.responses.input_tokens.count,
            "create": client.responses.create,
        }[stage]
        failing.side_effect = error
        with pytest.raises(RefreshFailure) as caught:
            OpenAIProposalModel(client, limits()).generate(model_input())
        assert caught.value.failure.code == code
        assert caught.value.failure.retryable
        failing.assert_called_once()
        assert client.responses.create.call_count == (1 if stage == "create" else 0)
    failing.side_effect = TypeError("programming defect")
    with pytest.raises(TypeError):
        OpenAIProposalModel(client, limits()).generate(model_input())


def test_adapter_with_real_service_keeps_all_or_nothing_and_saved_recovery():
    from app.refresh.models import Outcome
    from tests.refresh.test_real_service import real_scenario

    s = real_scenario()
    s.request = replace(
        s.request,
        model_settings=ModelSettings("gpt-5.6-luna", PROMPT_VERSION, CANDIDATE_VERSION),
    )
    client = client_fixture()
    good = client.responses.create.return_value
    bad = SimpleNamespace(status="incomplete", output=[], output_text="")
    client.responses.create.side_effect = [good, bad]
    s.service.model = OpenAIProposalModel(client, limits())
    done = s.service.run(s.request)
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == "MODEL_INCOMPLETE"
    result = s.artifacts.read_result(s.request)
    assert result.document_proposals == result.relation_proposals == ()
    assert s.service.run(s.request) == done
    assert client.responses.create.call_count == 2
