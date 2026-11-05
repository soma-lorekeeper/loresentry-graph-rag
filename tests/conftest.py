"""여러 테스트가 함께 쓰는 로그 줄 캡처와 OpenTelemetry SDK. 로그는 운영과 같은 ECS 포매터로 만든 JSON 줄을 읽는다.

SDK는 이미지에서 EDOT distro(docker-entrypoint.sh의 opentelemetry-instrument)가 하는 일을 대신한다.
- TracerProvider를 한 번 꽂고, 끝난 span을 메모리(EXPORTER)에 모은다. 테스트마다 비운다.
- 이미지와 같이 span 속성은 안정 이름(OTEL_SEMCONV_STABILITY_OPT_IN)으로 내고, distro가 켜는 계측(content 호출·Neptune
  쿼리의 httpx, S3·KMS의 botocore)을 켠다. FastAPI는 앱마다 app.logs.setup_tracing이 단다.
- spans fixture는 그 테스트에서 끝난 span 목록을 돌려주는 함수다.
- MeterProvider도 한 번 꽂고, 기록된 지표를 메모리 reader(METRICS)로 모은다. 이미지의 distro처럼 histogram은 delta라
  모을 때마다 앞에서 모은 뒤의 값만 나온다. 테스트를 시작할 때 한 번 모아 앞 테스트의 값을 버린다.
- measured fixture는 그 테스트에서 기록된 지표를 이름별로 돌려주는 함수다.
"""

import io
import json
import logging
import os
from types import SimpleNamespace

import pytest

# 계측을 import하기 전에 둔다. 계측이 처음 쓸 때 한 번만 읽는다
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")

from opentelemetry import metrics, trace  # noqa: E402
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402
from opentelemetry.sdk.metrics import Histogram, MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import (  # noqa: E402
    AggregationTemporality,
    InMemoryMetricReader,
)
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
# 기록된 지표를 모으는 reader. distro(OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta)와 같이 histogram은 delta다.
# app.logs의 지표는 import 때 API의 대리 객체로 만들어졌다가 여기서 꽂은 provider로 옮겨 간다
METRICS = InMemoryMetricReader(
    preferred_temporality={Histogram: AggregationTemporality.DELTA}
)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))
HTTPXClientInstrumentor().instrument()
BotocoreInstrumentor().instrument()


@pytest.fixture(autouse=True)
def _clear_spans():
    EXPORTER.clear()


@pytest.fixture
def spans():
    """그 테스트에서 지금까지 끝난 span 목록(끝난 순서)을 돌려주는 함수."""
    return lambda: list(EXPORTER.get_finished_spans())


@pytest.fixture(autouse=True)
def _clear_metrics():
    # 앞 테스트가 기록한 histogram 값을 버린다(delta라 한 번 모으면 비워진다)
    METRICS.get_metrics_data()


@pytest.fixture
def measured():
    """그 테스트에서 기록된 지표를 모아 {이름: (scope, unit, points)}로 돌려주는 함수.

    histogram은 부를 때마다 앞의 호출(또는 테스트 시작) 뒤에 기록된 값만 나온다. gauge는 부른 때의 값이다.
    points는 SDK의 data point(attributes, histogram이면 count·sum·min·max·explicit_bounds·exemplars, gauge면 value)다.
    """

    def read() -> dict[str, SimpleNamespace]:
        found = {}
        data = METRICS.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    found[metric.name] = SimpleNamespace(
                        scope=scope.scope.name,
                        unit=metric.unit,
                        points=list(metric.data.data_points),
                    )
        return found

    return read


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
