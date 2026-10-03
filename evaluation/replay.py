"""저장된 대화 작성 응답을 실제 후보 파서에 전달하는 평가 전용 모델 대체 구현."""

import json
from pathlib import Path

from app.adapters.llm_schema import parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, Usage


class ResponseReplay:
    """입력 fingerprint·버전·대상이 일치할 때만 원본 JSON을 재생한다."""

    def __init__(self, directory: Path, case, recorder):
        self.directory, self.case, self.recorder = directory, case, recorder

    def generate(self, model_input):
        target = "+".join(model_input.target_ids) or "relations"
        path = self.directory / f"{self.case.name}--{target}.json"
        try:
            envelope = json.loads(path.read_text())
        except (OSError, ValueError):
            raise RefreshFailure(
                Failure("REPLAY_INPUT_INVALID", "Response file unavailable or invalid")
            ) from None
        expected = {
            "input_fingerprint": self.case.request.job.input_fingerprint,
            "target_ids": list(model_input.target_ids),
            "prompt_version": model_input.prompt_version,
            "schema_version": model_input.schema_version,
            "response_source": "conversation-assistant",
        }
        if not isinstance(envelope, dict) or any(
            envelope.get(k) != v for k, v in expected.items()
        ):
            raise RefreshFailure(
                Failure(
                    "REPLAY_INPUT_MISMATCH", "Response does not match execution input"
                )
            )
        text = envelope.get("response_text")
        if not isinstance(text, str):
            raise RefreshFailure(
                Failure("REPLAY_INPUT_INVALID", "Response text required")
            )
        self.recorder.current.update(
            response_source="conversation-assistant",
            raw_candidate_text=text,
            response_file=str(path),
        )
        self.recorder.flush()
        # Synthetic zeros satisfy the internal Usage type, not billing evidence.
        # Recorder usage remains null/unknown and no provider model is asserted.
        return parse_candidate(text, Usage(1))
