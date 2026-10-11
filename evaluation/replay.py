"""저장된 대화 작성 응답을 실제 후보 파서에 전달하는 평가 전용 모델 대체 구현."""

import json
from dataclasses import asdict, replace
from pathlib import Path

from app.adapters.llm_schema import parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentBatch, Failure, Outcome, Usage
from app.refresh.rules import RefreshRules
from app.refresh.validation import relation_in_scope


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
        legacy_version = (
            (
                envelope.get("schema_version"),
                envelope.get("prompt_version"),
            )
            if isinstance(envelope, dict)
            else None
        )
        if legacy_version in {
            ("refresh-candidate-v1", "refresh-prompt-v2"),
            ("refresh-candidate-v2", "refresh-prompt-v3"),
            ("refresh-candidate-v3", "refresh-prompt-v4"),
            ("refresh-candidate-v3", "refresh-prompt-v5"),
            ("refresh-candidate-v3", "refresh-prompt-v6"),
        }:
            # Explicit read compatibility for historical evaluation artifacts;
            # these responses do not validate current generation quality.
            expected.update(
                schema_version=legacy_version[0],
                prompt_version=legacy_version[1],
            )
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
        self.recorder.current["replayed_schema_version"] = envelope["schema_version"]
        self.recorder.flush()
        candidate = parse_candidate(
            text,
            Usage(1),
            schema_version=envelope["schema_version"],
            model_input=model_input,
        )
        if (
            legacy_version
            in {
                ("refresh-candidate-v1", "refresh-prompt-v2"),
                ("refresh-candidate-v2", "refresh-prompt-v3"),
                ("refresh-candidate-v3", "refresh-prompt-v4"),
            }
            and model_input.relation_scope is not None
        ):
            # Historical responses predate pair ownership. Validate their complete
            # original candidate first, then assign fixed relations to their owner.
            # Preserve raw text above and explicitly record this replay adaptation.
            self.recorder.current["candidate"] = asdict(candidate)
            self.recorder.current["legacy_original_candidate"] = asdict(candidate)
            self.recorder.flush()
            rules = RefreshRules()
            selection = rules.select(
                self.case.request, self.case.source, self.case.related
            )
            fetched = DocumentBatch(
                tuple(
                    d
                    for d in self.case.targets
                    if d.document_id in selection.missing_ids
                )
            )
            snapshot = rules.assemble(
                self.case.request,
                self.case.source,
                self.case.related,
                selection,
                fetched,
            )
            checked = rules.validate_candidate(
                snapshot, candidate, replace(model_input, relation_scope=None)
            )
            if checked.outcome == Outcome.FAILED:
                raise RefreshFailure(checked.failure)
            owned = tuple(
                p
                for p in candidate.relation_proposals
                if relation_in_scope(snapshot, p, model_input.relation_scope)
            )
            self.recorder.current["legacy_relation_scope_adapted"] = True
            self.recorder.flush()
            candidate = replace(candidate, relation_proposals=owned)
        return candidate
