"""동기 Responses 호출 경계. 클라이언트 소유·종료는 조립 지점의 책임이다."""

import json
import math
from dataclasses import dataclass

from openai import OpenAI

from app.adapters.llm_schema import (
    CANDIDATE_VERSION,
    PROMPT_VERSION,
    candidate_schema,
    parse_candidate,
    response_usage,
)
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, ModelCandidate, ModelInput

# Official model limits verified 2026-10-03; add models explicitly.
MODEL_CAPABILITIES = {"gpt-5.6-luna": (922000, 128000, 1050000)}


@dataclass(frozen=True)
class ModelLimits:
    """운영자가 선택 모델 한도 안으로 설정한 토큰·시간 예산."""

    model: str
    timeout_seconds: float
    max_input_tokens: int
    max_output_tokens: int
    context_window: int

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


def fail(code: str, message: str, retryable: bool = False) -> None:
    """공급자 원문을 노출하지 않는 실패 값으로 변환한다."""
    raise RefreshFailure(Failure(code, message, retryable))


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
            or model_input.schema_version != CANDIDATE_VERSION
            or model_input.prompt_version != PROMPT_VERSION
            or not model_input.instructions.strip()
            or not model_input.prompt.strip()
            or self.client.max_retries != 0
        ):
            fail("MODEL_REQUEST_INVALID", "Unsupported model input configuration")
        schema = candidate_schema()
        # UTF-8 byte length is a conservative text token estimate, plus framing
        # allowance. It is intentionally stricter than a tokenizer, never truncates.
        estimate = (
            sum(
                len(s.encode("utf-8"))
                for s in (
                    model_input.instructions,
                    model_input.prompt,
                    json.dumps(schema),
                )
            )
            + 4096
        )
        if estimate > self.limits.max_input_tokens:
            fail("MODEL_REQUEST_INVALID", "Model input token budget exceeded")
        response = self.client.responses.create(
            model=model_input.model,
            instructions=model_input.instructions,
            input=[{"role": "user", "content": model_input.prompt}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "refresh_candidate",
                    "strict": True,
                    "schema": schema,
                }
            },
            max_output_tokens=self.limits.max_output_tokens,
            timeout=self.limits.timeout_seconds,
            store=False,
            truncation="disabled",
        )
        return parse_candidate(
            response.output_text,
            response_usage(response.usage.model_dump() if response.usage else None),
        )
