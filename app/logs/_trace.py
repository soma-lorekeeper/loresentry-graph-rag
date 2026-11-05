"""로깅 팩토리가 우리 span을 열고, 지금 span을 로그·저장소·Kafka 헤더가 쓰는 모양으로 꺼내는 함수(LOG_SCHEMA.md 7절,
APM_ARCHITECTURE.md 3절). ai-chat app/logs/_trace.py와 같은 함수·규칙이다.

추적 엔진은 EDOT Python distro(``elastic-opentelemetry``)다. 이미지의 시작 스크립트(docker-entrypoint.sh)가 uvicorn을
``opentelemetry-instrument``로 띄우면, distro가 프로세스가 시작할 때 SDK TracerProvider·exporter·전파기를 만들고 설치된
계측(httpx: content 호출과 Neptune 쿼리, botocore: S3·KMS)을 켠다. OpenAI 호출은 계측이 없어 ``LogFlow.call``의 CLIENT span이
맡고, Kafka(confluent-kafka)도 계측 없이 ``LogFlow.kafka_record``(소비)와 ``LogFlow.call("kafka", ...)``(발행)가 맡는다.
그래서 이 모듈은 SDK를 설정하지 않고 OpenTelemetry API만 쓴다. distro 없이 띄운 곳(로컬 실행, 평가 CLI)에서는 API가 아무것도
하지 않아 trace ID도 없고, 테스트는 tests/conftest.py가 꽂은 SDK를 쓴다.

- ``setup_tracing``: API 앱 하나에 FastAPI 계측을 단다. distro의 FastAPI 자동 계측은 끄고(OTEL_PYTHON_DISABLED_INSTRUMENTATIONS)
  여기서 내부 ASGI span(``receive``·``send``)을 빼고 단다.
- 전파 형식은 W3C ``traceparent`` 하나다. 저장해 둔 값(Neptune InboxEvent의 traceparent)과 Kafka 헤더는 전역 전파기 설정과
  상관없이 W3C 전파기(``W3C``)로 읽고 쓴다.
- 나머지 함수는 로깅 팩토리(``_logger``, ``_flow``, ``_format``, ``_request``)가 쓰는 내부 함수다. 서비스 코드는 추적 API를 쓰지
  않는다(ruff banned-api가 ``opentelemetry``를 이 패키지 밖에서 막는다).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Link, Span, SpanContext, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.logs._fields import _class_name

# 이 패키지가 여는 span의 계측 이름(scope.name). gitops logging/tools/apm_consistency.py가 이 이름으로 우리 span을 알아본다
TRACER = "app.logs"
# 할 일이 없던 폴링 회차의 span 표시. EDOT Collector가 이 속성이 true인 span이 든 trace를 통째로 버린다
# (gitops logging/base/otel-collector/otel.yml의 tail_sampling, LOG_SCHEMA.md 7절)
IDLE_POLLING = "loresentry.polling.idle"
# 저장해 둔 값·Kafka 헤더를 읽고 쓰는 W3C 전파기. traceparent만 다룬다(version 00, 32·16 hex, 0이 아닌 ID인지 확인한다)
W3C = TraceContextTextMapPropagator()


def setup_tracing(app: Any) -> None:
    """API 앱에 FastAPI 계측을 단다. 요청마다 서버 span을 열고, 보낸 쪽의 traceparent가 있으면 그 trace를 잇는다.

    계측의 middleware가 앱의 가장 바깥에 붙어, 요청 로그 middleware(RequestLogMiddleware)와 route가 그 서버 span 안에서 돈다.
    응답 조각마다 생기는 내부 ASGI span(``receive``·``send``)은 만들지 않는다(span 점검 4번). 앱 하나에 한 번 부른다.
    """
    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])


def traceparent() -> str | None:
    """지금 span의 W3C traceparent(`00-<trace>-<span>-<flags>`, 55자). span이 없으면 None.

    저장했다 나중에 처리하는 일(Neptune InboxEvent의 traceparent 속성)에 함께 저장하면, 나중에 그 일을 처리하는 쪽(relay)의
    로깅 팩토리가 이 값을 부모로 span을 열어 지금 흐름의 trace를 잇는다. 로그가 아니다.
    """
    span = trace.get_current_span().get_span_context()
    if not span.is_valid:
        return None
    return f"00-{span.trace_id:032x}-{span.span_id:016x}-{int(span.trace_flags):02x}"


def current_ids() -> tuple[str | None, str | None]:
    """지금 span의 (trace.id, span.id). span이 없으면 (None, None)."""
    return span_ids(trace.get_current_span())


def span_ids(span: Span) -> tuple[str | None, str | None]:
    """span 하나의 (trace.id, span.id). 유효하지 않은 span(추적 엔진이 없을 때)이면 (None, None)."""
    context = span.get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def current_span() -> SpanContext:
    """지금 span의 문맥. span이 없으면 유효하지 않은 문맥이다."""
    return trace.get_current_span().get_span_context()


def stored_span(value: str | None) -> SpanContext:
    """저장해 둔 traceparent를 원래 쪽(그 값을 저장한 레코드 처리)의 span 문맥으로 읽는다. 값이 없거나 틀리면 유효하지 않은 문맥."""
    if not value:
        return trace.INVALID_SPAN_CONTEXT
    extracted = W3C.extract({"traceparent": value}, context=Context())
    return trace.get_current_span(extracted).get_span_context()


def header_parent(headers: Mapping[str, str]) -> Context:
    """Kafka 헤더의 traceparent를 빈 문맥에서 꺼낸다. 지금 문맥(다른 흐름의 span)이 섞이지 않는다.

    traceparent가 없거나 형식이 틀리면 빈 문맥이라, 그 문맥으로 연 span이 새 trace의 root가 된다(LOG_SCHEMA.md 7절 "시작점").
    """
    return W3C.extract(headers, context=Context())


def fill_headers(headers: list[tuple[str, bytes | None]]) -> None:
    """발행할 Kafka 헤더 목록에 지금 span의 traceparent를 넣는다(LOG_SCHEMA.md 7절 "Kafka 발행").

    목록에 이미 같은 이름의 헤더(DLQ로 보내는 원본 레코드의 traceparent 등)가 있으면 지우고 넣는다. 그래서 헤더의 trace는 늘
    지금 문맥(발행 span)이고, 받는 쪽이 읽는 traceparent는 하나다. span이 없으면(추적 엔진이 없을 때) 아무것도 넣지 않는다.
    """
    carrier: dict[str, str] = {}
    W3C.inject(carrier)
    if not carrier:
        return
    headers[:] = [(name, value) for name, value in headers if name not in carrier]
    headers.extend((name, value.encode()) for name, value in carrier.items())


def start_span(
    name: str,
    kind: trace.SpanKind,
    parent: Context | None,
    attributes: Mapping[str, Any] | None = None,
    links: Sequence[Link] = (),
) -> Span:
    """이 패키지의 tracer로 span을 시작한다. parent가 None이면 지금 문맥(지금 span의 자식, 없으면 root)이다.

    tracer는 부를 때마다 꺼낸다. 실행 중에는 distro의 provider이고, 테스트가 꽂은 provider도 그대로 쓴다.
    """
    return trace.get_tracer(TRACER).start_span(
        name, context=parent, kind=kind, attributes=attributes, links=links
    )


def stored_parent(stored: SpanContext) -> Context:
    """저장된 span을 부모로 둔 빈 문맥. 유효하지 않으면 빈 문맥이라 그 문맥으로 연 span이 새 trace의 root다."""
    if not stored.is_valid:
        return Context()
    return trace.set_span_in_context(trace.NonRecordingSpan(stored), Context())


def activate(span: Span) -> object:
    """이 패키지가 연 span을 지금 문맥의 span으로 둔다. 그 안에서 계측이 여는 span(Neptune·content HTTP, S3·KMS)이 이 span의
    자식이 된다. 돌려준 token으로 뗀다."""
    return otel_context.attach(trace.set_span_in_context(span))


def detach(token: object) -> None:
    otel_context.detach(token)


def mark_failed(
    span: Span, error_type: str | None, error: BaseException | None
) -> None:
    """우리 span을 실패로 표시한다(상태 ERROR, 설명은 그 줄의 error.type).

    예외로 끝났으면 예외 event를 남기되 종류(exception.type)만 넣는다. 예외 메시지와 스택은 span에 싣지 않는다(LOG_SCHEMA.md 4절
    "trace(span) 마스킹"). 스택은 같은 span.id의 실패 줄 error.stack_trace로 본다.
    """
    span.set_status(Status(StatusCode.ERROR, error_type))
    if isinstance(error, Exception):
        # 클래스 이름은 메시지 없는 스택의 첫 줄과 같은 모양이다(내장 예외는 이름만, 나머지는 모듈.클래스)
        span.add_event("exception", {"exception.type": _class_name(type(error))})
