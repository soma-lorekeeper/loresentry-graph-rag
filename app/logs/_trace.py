"""OpenTelemetry span을 로그 시작점과 저장용 traceparent 모양으로 다룬다(LOG_SCHEMA.md 7절).

span은 계측 라이브러리(FastAPI 서버 span, content httpx 호출 span)와 로깅 팩토리(Kafka 레코드 하나의 consumer span,
스케줄·폴링 회차의 span)가 연다. 환경 변수 OTEL_EXPORTER_OTLP_ENDPOINT(EDOT Collector 주소)가 있을 때만 span을
OTLP/HTTP로 보낸다. 보내지 않아도 trace·span ID는 만들어지고 HTTP·Kafka 헤더로 전파된다.
"""

import os
import re

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

# W3C traceparent version 00: 00-<trace-id 32 hex>-<parent span-id 16 hex>-<flags 2 hex>
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
# 모든 값이 0인 ID는 W3C에서 무효다
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16
# 로깅 팩토리가 여는 span의 instrumentation scope 이름
_TRACER = "app.logs"

_configured = False


def setup_tracing(service_name: str) -> None:
    """SDK TracerProvider를 전역으로 둔다. 여러 번 불러도 처음 한 번만 적용한다.

    resource는 service.name과 service.version(APP_VERSION, 로그와 같은 이미지 태그)이고, Resource.create가
    OTEL_RESOURCE_ATTRIBUTES(예: deployment.environment.name=dev, Kibana APM의 환경 구분)를 함께 합친다.
    sampler는 ParentBased(AlwaysOn)다(부모가 표본이면 따르고, 시작점은 모두 남긴다). 전파(propagator)는 OpenTelemetry
    기본값(W3C tracecontext)을 쓴다.

    OTEL_EXPORTER_OTLP_ENDPOINT가 있으면(dev overlay가 collector 주소를 넣는다) span을 묶어 OTLP/HTTP로 보낸다. 없으면(로컬 실행,
    테스트) 보내지 않는다. 보내지 못한 span은 버려지고 요청·레코드 처리는 멈추지 않는다.
    """
    global _configured
    if _configured:
        return
    attributes = {SERVICE_NAME: service_name}
    if version := os.environ.get("APP_VERSION"):
        attributes[SERVICE_VERSION] = version
    provider = TracerProvider(
        resource=Resource.create(attributes),
        sampler=ParentBased(ALWAYS_ON),
    )
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # exporter가 같은 환경 변수에서 주소를 읽고 /v1/traces를 붙인다
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def current_ids() -> tuple[str | None, str | None]:
    """지금 열려 있는 span의 (trace_id, span_id). span이 없으면 (None, None)."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def span_ids(span: Span) -> tuple[str | None, str | None]:
    """span 하나의 (trace_id, span_id). 유효하지 않은 span이면 (None, None)."""
    context = span.get_span_context()
    if not context.is_valid:
        return None, None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def traceparent() -> str | None:
    """지금 span의 W3C traceparent(`00-<trace>-<span>-<flags>`, 55자). span이 없으면 None.

    저장했다 나중에 처리하는 일(graph-rag Neptune InboxEvent의 traceparent 속성)에 함께 저장하면, 나중에 그 일을
    처리하는 쪽(relay)이 이 값으로 Kafka 헤더를 붙여 받는 서비스가 지금 흐름의 trace를 잇는다. 로그가 아니다.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return (
        f"00-{context.trace_id:032x}-{context.span_id:016x}"
        f"-{int(context.trace_flags):02x}"
    )


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """저장해 둔 traceparent의 (trace_id, span_id). 값이 없거나 형식이 틀리면 None."""
    if not isinstance(value, str):
        return None
    matched = _TRACEPARENT.fullmatch(value)
    if matched is None or _ZERO_TRACE == matched.group(1):
        return None
    if _ZERO_SPAN == matched.group(2):
        return None
    return matched.group(1), matched.group(2)


def start_consumer_span(
    name: str, parent_traceparent: str | None, attributes: dict
) -> Span:
    """Kafka 레코드 하나의 consumer span. 헤더의 traceparent가 부모이고, 없거나 틀리면 새 trace를 시작한다.

    지금 스레드의 span을 부모로 쓰지 않는다. 처리 스레드는 레코드마다 새로 시작하기 때문이다.
    """
    carrier = {} if parent_traceparent is None else {"traceparent": parent_traceparent}
    parent = extract(carrier, context=otel_context.Context())
    return trace.get_tracer(_TRACER).start_span(
        name, context=parent, kind=SpanKind.CONSUMER, attributes=attributes
    )


def start_root_span(name: str) -> Span:
    """스케줄·폴링 회차 하나의 span. 늘 새 trace를 시작한다(회차마다 새 trace, LOG_SCHEMA.md 7절 시작점)."""
    return trace.get_tracer(_TRACER).start_span(
        name, context=otel_context.Context(), kind=SpanKind.INTERNAL
    )


def attach(span: Span) -> object:
    """span을 지금 문맥의 현재 span으로 둔다. 돌려준 token으로 detach한다."""
    return otel_context.attach(trace.set_span_in_context(span))


def detach(token: object) -> None:
    otel_context.detach(token)


def mark_failed(span: Span) -> None:
    """span을 실패로 표시한다. 예외 메시지가 span 속성에 들어가지 않도록 설명은 붙이지 않는다."""
    span.set_status(Status(StatusCode.ERROR))
