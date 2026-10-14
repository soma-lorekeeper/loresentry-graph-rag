"""평가 원본·후보·실패 기록. 로그 대신 접근 제한된 로컬 JSON에 저장한다."""

import json
import os
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError
from openai import __version__ as sdk_version

from app.refresh.errors import RefreshFailure


def write_json(path: Path, value) -> None:
    """파일 모드 0600으로 쓰고 교체하여 중간 JSON을 노출하지 않는다."""
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class AttemptRecorder:
    """시도마다 원본 응답과 파싱 결과를 즉시 저장한다. 원격 재시도는 하지 않는다."""

    def __init__(
        self,
        directory: Path,
        case,
        limits,
        live: bool,
        *,
        observe_sources=False,
        observe_confidence=False,
    ):
        self.directory, self.case, self.limits, self.live = (
            directory,
            case,
            limits,
            live,
        )
        self.records = []
        self.current = None
        self.observe_sources = observe_sources
        self.observe_confidence = observe_confidence
        self.last_candidate = None

    def flush(self, record=None):
        record = self.current if record is None else record
        write_json(
            self.directory / f"{self.case.name}-attempt-{record['attempt']}.json",
            record,
        )

    def reconcile(self, attempts):
        """서비스의 검증 결과를 같은 순서의 모델 기록에 연결한다. 재호출은 없다."""
        for record, attempt in zip(self.records, attempts, strict=False):
            record["validation"] = asdict(attempt)
            if attempt.failure is not None:
                record["failure"] = asdict(attempt.failure)
            self.flush(record)

    def generate(self, model_input, call):
        self.last_candidate = None
        try:
            prompt = json.loads(model_input.prompt)
        except (TypeError, ValueError):
            prompt = {}
        repair = prompt.get("validation_repair", {}) if isinstance(prompt, dict) else {}
        repair = repair if isinstance(repair, dict) else {}
        attempt_index = repair.get("attempt", 0)
        if type(attempt_index) is not int or attempt_index < 0:
            attempt_index = 0
        call_index = (
            self.records[-1]["call_index"]
            if attempt_index and self.records
            else sum(record["attempt_index"] == 0 for record in self.records) + 1
        )
        record = {
            "case": self.case.name,
            "request": asdict(self.case.request.job),
            "attempt": len(self.records) + 1,
            "call_index": call_index,
            "attempt_index": attempt_index,
            "repair_version": repair.get("version"),
            "sdk_version": sdk_version,
            "live": self.live,
            "settings": asdict(self.limits),
            "model_input": asdict(model_input),
            "stage": "PLAN"
            if model_input.schema_version == "refresh-plan-v1"
            else "WRITE"
            if model_input.schema_version
            in {"refresh-candidate-v7", "refresh-candidate-v8"}
            else "GENERATE",
            "instruction_fingerprint": sha256(
                model_input.instructions.encode()
            ).hexdigest(),
            "raw_response": None,
            "candidate": None,
            "failure": None,
            "usage": None,
            "usage_known": False,
            "remote_requested": False,
        }
        self.records.append(record)
        self.current = record
        self.flush()
        started = time.perf_counter()
        try:
            candidate = call()
            self.last_candidate = candidate
            record["candidate"] = asdict(candidate)
            # Offline fake token counts are not evidence of paid usage.
            return candidate
        except RefreshFailure as error:
            record["failure"] = asdict(error.failure)
            raise
        except Exception as error:
            record["unexpected_error_type"] = type(error).__name__
            raise
        finally:
            record["elapsed_seconds"] = time.perf_counter() - started
            if self.observe_sources and "source_observation" not in record:
                # Business validation runs after generate; parsed candidates are
                # observed there. Parse/transport failures remain unavailable.
                if record["candidate"] is None:
                    record["source_observation"] = {
                        "status": "unavailable",
                        "reason": (record["failure"] or {}).get("code", "no_candidate"),
                    }
            if self.observe_confidence and record["candidate"] is None:
                record["confidence_observation"] = {
                    source: {"status": "unavailable", "reason": "candidate_unparsed"}
                    for source in ("raw", "effective")
                }
            self.flush()


class RecordingClient:
    """SDK 호출만 감싸 원본을 파싱 전에 보존한다. 실제 SDK 자원은 호출자가 소유한다."""

    def __init__(self, client, recorder: AttemptRecorder):
        self.client, self.recorder = client, recorder
        self.responses = SimpleNamespace(create=self.create)

    @property
    def max_retries(self):
        return self.client.max_retries

    def create(self, **kwargs):
        record = self.recorder.current
        record["remote_requested"] = True
        self.recorder.flush()
        try:
            response = self.client.responses.create(**kwargs)
        except APIStatusError as error:
            record["http_status"] = error.status_code
            record["provider_request_id"] = error.request_id
            record["raw_error"] = error.body
            self.recorder.flush()
            raise
        except APIConnectionError:
            # A timeout may still have incurred usage. Unknown is not zero.
            self.recorder.flush()
            raise
        record["raw_response"] = response.model_dump(mode="json")
        record["provider_request_id"] = response._request_id
        record["response_id"] = response.id
        record["response_model"] = response.model
        record["usage"] = response.usage.model_dump() if response.usage else None
        record["usage_known"] = response.usage is not None
        self.recorder.flush()
        return response
