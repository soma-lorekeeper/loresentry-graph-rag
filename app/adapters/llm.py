"""동기 Responses 호출 경계. 클라이언트 소유·종료는 조립 지점의 책임이다."""

import math
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.adapters.llm_schema import (
    CANDIDATE_VERSION,
    GENERATION_VERSIONS,
    candidate_schema,
    parse_candidate,
    response_usage,
)
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, ModelCandidate, ModelInput, Usage
from app.refresh.prompt_versions import SUPPORTED_PROMPT_VERSIONS

# Official model limits: GPT-5.6 Luna 2026-10-03, GPT-6 Luna 2026-10-11.
MODEL_CAPABILITIES = {
    "gpt-5.6-luna": (922000, 128000, 1050000),
    "gpt-6-luna": (922000, 128000, 1050000),
}

# 호출 하나의 입력은 모델 최대 입력의 이 비율까지만 쓴다. 문서 수·본문 길이 대신 이 예산이 상한이다.
INPUT_BUDGET_RATIO = 0.8


@dataclass(frozen=True)
class ModelLimits:
    """운영자가 선택 모델 한도 안으로 설정한 토큰·시간 예산."""

    model: str
    timeout_seconds: float
    max_input_tokens: int
    max_output_tokens: int
    context_window: int

    @property
    def input_budget(self) -> int:
        """호출 하나에 보낼 수 있는 입력 토큰. OpenAI 계수 API가 센 값과 비교한다."""
        return int(self.max_input_tokens * INPUT_BUDGET_RATIO)

    def validate(self) -> None:
        """잘못된 예산을 네트워크 호출 전에 거절한다."""
        caps = MODEL_CAPABILITIES.get(self.model)
        if (
            caps is None
            or not self.model.strip()
            or self.model == "unconfigured"
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or any(
                type(n) is not int or n <= 0
                for n in (
                    self.max_input_tokens,
                    self.max_output_tokens,
                    self.context_window,
                )
            )
            or self.max_input_tokens + self.max_output_tokens > self.context_window
            or self.max_input_tokens > caps[0]
            or self.max_output_tokens > caps[1]
            or self.context_window > caps[2]
        ):
            fail("MODEL_REQUEST_INVALID", "Invalid model limits")


def fail(
    code: str,
    message: str,
    retryable: bool = False,
    *,
    usage: Usage | None = None,
    candidate_text: str | None = None,
) -> None:
    """공급자 원문을 노출하지 않는 실패 값으로 변환한다."""
    raise RefreshFailure(
        Failure(code, message, retryable),
        usage=usage if usage is not None else Usage(),
        candidate_text=candidate_text,
    ) from None


class OpenAIProposalModel:
    """주입한 클라이언트로 후보를 생성한다. 자체 재시도나 자원 종료는 하지 않는다."""

    def __init__(self, client: OpenAI, limits: ModelLimits):
        limits.validate()
        if client.max_retries != 0:
            fail("MODEL_REQUEST_INVALID", "SDK retries must be disabled")
        self.client = client
        self.limits = limits

    def generate(self, model_input: ModelInput) -> ModelCandidate:
        """호출당 최대 한 요청을 보내고 응답을 업무 검증 전 후보로 반환한다."""
        self.limits.validate()
        if (
            model_input.model != self.limits.model
            or model_input.schema_version not in GENERATION_VERSIONS
            or model_input.prompt_version not in SUPPORTED_PROMPT_VERSIONS
            or not model_input.instructions.strip()
            or not model_input.prompt.strip()
            or self.client.max_retries != 0
        ):
            fail("MODEL_REQUEST_INVALID", "Unsupported model input configuration")
        # 모델이 실제로 받을 토큰을 세어 예산을 넘으면 생성 요청을 보내지 않는다. 원문은 자르지 않는다.
        if self.count(model_input) > self.limits.input_budget:
            fail("MODEL_REQUEST_INVALID", "Model input token budget exceeded")
        try:
            response = self.client.responses.create(
                **request_arguments(model_input),
                max_output_tokens=self.limits.max_output_tokens,
                timeout=self.limits.timeout_seconds,
                store=False,
                truncation="disabled",
            )
        except APITimeoutError:
            fail("MODEL_TIMEOUT", "Model request timed out", True)
        except APIConnectionError:
            fail("MODEL_CONNECTION_FAILED", "Model connection failed", True)
        except APIStatusError as error:
            raise RefreshFailure(status_failure(error), usage=Usage(calls=1)) from None
        return decode_response(response, model_input=model_input)

    def count(self, model_input: ModelInput) -> int:
        """생성과 같은 인자로 OpenAI input_tokens 계수 API를 불러 입력 토큰을 센다.

        역할·경계 같은 형식 토큰까지 모델이 받는 정확한 수다. 생성 호출이 아니므로 사용량에 넣지 않는다.

        Raises:
            RefreshFailure: 통신 실패는 재시도 가능, 거절·잘못된 응답은 재시도 불가로 분류한다.
        """
        try:
            response = self.client.responses.input_tokens.count(
                **request_arguments(model_input), timeout=self.limits.timeout_seconds
            )
        except APITimeoutError:
            fail("MODEL_TIMEOUT", "Model token count timed out", True)
        except APIConnectionError:
            fail("MODEL_CONNECTION_FAILED", "Model connection failed", True)
        except APIStatusError as error:
            raise RefreshFailure(status_failure(error)) from None
        tokens = getattr(response, "input_tokens", None)
        if type(tokens) is not int or tokens < 0:
            fail("MODEL_INVALID_RESPONSE", "Model token count is invalid")
        return tokens


def request_arguments(model_input: ModelInput) -> dict:
    """생성과 토큰 계수가 같은 입력을 보도록 공유하는 Responses 인자."""
    return {
        "model": model_input.model,
        "instructions": model_input.instructions,
        "input": [{"role": "user", "content": model_input.prompt}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "refresh_candidate",
                "strict": True,
                "schema": candidate_schema(model_input.schema_version),
            }
        },
    }


def status_failure(error: APIStatusError) -> Failure:
    """기존 AI의 일시 오류·할당량 오류 분류를 Responses SDK 계약으로 옮긴다."""
    body = error.body if isinstance(error.body, dict) else {}
    body = body.get("error", body)
    code = body.get("code") if isinstance(body, dict) else None
    if code in {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "usage_limit_reached",
    }:
        return Failure("MODEL_QUOTA_EXHAUSTED", "Model quota exhausted")
    if error.status_code in {401, 403}:
        return Failure("MODEL_AUTH_FAILED", "Model authorization failed")
    if error.status_code == 429:
        return Failure("MODEL_RATE_LIMITED", "Model temporarily rate limited", True)
    if error.status_code >= 500 or error.status_code in {408, 409}:
        return Failure("MODEL_UNAVAILABLE", "Model temporarily unavailable", True)
    return Failure("MODEL_REQUEST_INVALID", "Model request rejected")


def decode_response(
    response, *, model_input: ModelInput | None = None
) -> ModelCandidate:
    """완료 상태·거절·본문을 검증한 뒤 DTO로 변환한다."""
    candidate_text = response.output_text
    raw_usage = getattr(response, "usage", None)
    usage_error = None
    try:
        usage = response_usage(raw_usage.model_dump() if raw_usage else None)
    except RefreshFailure as error:
        usage_error = error
        usage = Usage(calls=1)
    if response.status != "completed":
        fail(
            "MODEL_INCOMPLETE",
            "Model response incomplete",
            usage=usage,
            candidate_text=candidate_text,
        )
    for item in response.output:
        if item.type == "message" and any(
            part.type == "refusal" for part in item.content
        ):
            fail(
                "MODEL_REFUSED",
                "Model refused the request",
                usage=usage,
                candidate_text=candidate_text,
            )
    if not candidate_text or not candidate_text.strip():
        fail(
            "MODEL_EMPTY_RESPONSE",
            "Model returned no candidate",
            usage=usage,
            candidate_text=candidate_text,
        )
    if usage_error is not None:
        raise RefreshFailure(
            usage_error.failure, usage=usage, candidate_text=candidate_text
        ) from None
    return parse_candidate(
        candidate_text,
        usage,
        model_input=model_input,
        schema_version=(
            model_input.schema_version if model_input is not None else CANDIDATE_VERSION
        ),
    )
