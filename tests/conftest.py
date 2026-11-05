"""여러 테스트가 함께 쓰는 로그 줄 캡처와 OpenTelemetry SDK. 로그는 운영과 같은 ECS 포매터로 만든 JSON 줄을 읽는다.

SDK는 이미지에서 EDOT distro(docker-entrypoint.sh의 opentelemetry-instrument)가 하는 일을 대신한다.
- TracerProvider를 한 번 꽂고, 끝난 span을 메모리(EXPORTER)에 모은다. 테스트마다 비운다.
- 이미지와 같이 span 속성은 안정 이름(OTEL_SEMCONV_STABILITY_OPT_IN)으로 내고, distro가 켜는 계측(content 호출·Neptune
  쿼리의 httpx, S3·KMS의 botocore)을 켠다. FastAPI는 앱마다 app.logs.setup_tracing이 단다.
- spans fixture는 그 테스트에서 끝난 span 목록을 돌려주는 함수다.
"""

import io
import json
import logging
import os

import pytest

# 계측을 import하기 전에 둔다. 계측이 처음 쓸 때 한 번만 읽는다
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")

from opentelemetry import trace  # noqa: E402
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from app.logs._format import EcsFormatter  # noqa: E402

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)
HTTPXClientInstrumentor().instrument()
BotocoreInstrumentor().instrument()


@pytest.fixture(autouse=True)
def _clear_spans():
    EXPORTER.clear()


@pytest.fixture
def spans():
    """그 테스트에서 지금까지 끝난 span 목록(끝난 순서)을 돌려주는 함수."""
    return lambda: list(EXPORTER.get_finished_spans())


class CapturedLines:
    """테스트 동안 root logger가 받은 줄. 운영 포매터가 만든 JSON 그대로다."""

    def __init__(self, stream: io.StringIO):
        self._stream = stream

    def text(self) -> str:
        """줄 전체의 원문. 비밀 값이 어디에도 없는지 볼 때 쓴다."""
        return self._stream.getvalue()

    def documents(self) -> list[dict]:
        """줄마다 중첩 JSON 객체."""
        return [json.loads(line) for line in self.text().splitlines() if line]

    def __call__(self) -> list[dict]:
        """줄마다 점 이름으로 편 dict({"log.level": "INFO", ...})."""
        return [flatten(document) for document in self.documents()]

    def find(self, message: str) -> list[dict]:
        """문구가 같은 줄들(점 이름으로 편 dict)."""
        return [line for line in self() if line["message"] == message]

    def messages(self) -> list[str]:
        return [line["message"] for line in self()]


def flatten(document: dict, prefix: str = "") -> dict:
    """중첩 객체를 점 이름 dict로 편다. 배열(tags)은 그대로 둔다."""
    flat = {}
    for key, value in document.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, name + "."))
        else:
            flat[name] = value
    return flat


@pytest.fixture
def log_lines():
    """root logger에 운영 포매터를 단 handler를 붙여 이 테스트의 줄을 모은다."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(EcsFormatter("graph-rag-api", "test-version"))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield CapturedLines(stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
