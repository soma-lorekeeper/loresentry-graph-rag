"""평가 원본·후보·실패 기록. 로그 대신 접근 제한된 로컬 JSON에 저장한다."""

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError

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

    def __init__(self, directory: Path, case, limits, live: bool):
        self.directory, self.case, self.limits, self.live = (
            directory,
            case,
            limits,
            live,
        )
        self.records = []
        self.current = None

    def flush(self):
        write_json(
            self.directory / f"{self.case.name}-attempt-{len(self.records)}.json",
            self.current,
        )

    def generate(self, model_input, call):
        record = {
            "case": self.case.name,
            "request": asdict(self.case.request.job),
            "attempt": len(self.records) + 1,
            "live": self.live,
            "settings": asdict(self.limits),
            "model_input": asdict(model_input),
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
