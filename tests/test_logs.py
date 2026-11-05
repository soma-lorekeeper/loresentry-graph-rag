"""app/logs의 출력 형식·필드 변환·흐름 줄·trace 전파를 확인한다(LOG_SCHEMA.md 5절, 7절)."""

import json
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, StatusCode

from app.adapters.content_documents import ContentDocumentSource
from app.adapters.neptune_client import NeptuneGremlinClient
from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.kafka.consumer import BusinessConsumerHandler
from app.kafka.messages import REFRESH_COMPLETED, REFRESH_REQUESTED
from app.kafka.polling import consumer_config
from app.kafka.publisher import KafkaPublisher, producer_config
from app.logs import (
    AppLogger,
    LogField,
    LogFlow,
    LogScope,
    Outcome,
    RequestLogMiddleware,
    configure_logging,
    setup_tracing,
    traceparent,
)
from app.logs._fields import duration
from app.logs._format import EcsFormatter, _uncaught_in_thread, put
from app.logs._logger import _layer
from app.refresh.models import (
    ArtifactRef,
    Completion,
    JobKey,
    RefreshRequest,
)
from app.refresh.models import (
    Outcome as RefreshOutcome,
)
from app.refresh.outbox import RefreshOutboxRelay
from tests.test_kafka_messages import content_record
from tests.test_kafka_publisher import ProducerStub

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker-entrypoint.sh"
TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"
INCOMING = f"00-{TRACE}-{PARENT}-01"
PROJECT = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"
EVENT = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"
# 레코드 흐름 테스트의 consumer group
GROUP = "graph-rag.graph-refresh-requested"
# 계측이 여는 span(Neptune 쿼리, HTTP 호출)을 대신하는 테스트 자신의 span
TRACER = trace.get_tracer("tests")


class Color(Enum):
    RED = "red"


def ids(span) -> tuple[str, str]:
    """span의 (trace.id, span.id). 로그 줄이 쓰는 모양이다."""
    context = span.get_span_context()
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def line_ids(line: dict) -> tuple[str | None, str | None]:
    return line.get("trace.id"), line.get("span.id")


def named(found, name: str):
    """이름이 같은 끝난 span 하나."""
    (span,) = [span for span in found if span.name == name]
    return span


def traceparent_ids(value: str) -> tuple[str, str]:
    """traceparent 값의 (trace.id, span.id). flags는 SDK가 정한다(새 trace는 random 표시까지 03)."""
    version, trace_id, span_id, _flags = value.split("-")
    assert version == "00"
    return trace_id, span_id


def header_ids(headers) -> tuple[str, str]:
    """Kafka 헤더 목록의 traceparent(하나뿐이어야 한다)의 (trace.id, span.id)."""
    (value,) = [value for name, value in headers if name == "traceparent"]
    return traceparent_ids(value.decode())


@pytest.fixture
def loopback():
    """테스트 안의 HTTP 서버(127.0.0.1). 받은 요청(메서드, 경로, 소문자 헤더, 본문)을 모으고 respond가 돌려준 JSON으로 답한다.

    httpx 계측은 실제 transport(HTTPTransport)만 감싸므로, 나가는 요청의 traceparent는 이 서버로 확인한다.
    """
    state = SimpleNamespace(received=[], respond=lambda request: {})

    class Handler(BaseHTTPRequestHandler):
        def answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            request = SimpleNamespace(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=self.rfile.read(length) if length else b"",
            )
            state.received.append(request)
            payload = json.dumps(state.respond(request)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ 형식


def test_our_line_has_required_nested_fields(log_lines):
    AppLogger.current().info("graph refresh run", LogField.project_id(PROJECT))
    (document,) = log_lines.documents()
    # 점 이름이 아니라 중첩 객체다(Logstash가 점 이름 키를 필드로 읽지 못한다)
    assert document["log"]["level"] == "INFO"
    assert document["log"]["logger"] == __name__
    assert document["log"]["origin"]["function"] == (
        "test_our_line_has_required_nested_fields"
    )
    assert document["log"]["package"] == "tests"
    assert document["log"]["layer"] == "other"
    assert document["message"] == "graph refresh run"
    assert document["service"] == {"name": "graph-rag-api", "version": "test-version"}
    assert document["process"]["thread"]["name"] == threading.current_thread().name
    assert document["ecs"] == {"version": "8.11"}
    assert document["loresentry"]["project_id"] == PROJECT
    assert document["@timestamp"].endswith("Z") and "." in document["@timestamp"]


def test_levels_map_to_info_warn_error_for_ours_and_framework_lines(log_lines):
    AppLogger.current().warn("retry", LogField.error_type("X"))
    AppLogger.current().error("failed", LogField.error_type("X"))
    logging.getLogger("framework").warning("framework warning")
    logging.getLogger("framework").critical("framework critical")
    assert [line["log.level"] for line in log_lines()] == [
        "WARN",
        "ERROR",
        "WARN",
        "ERROR",
    ]


def test_framework_exception_has_type_and_messageless_stack(log_lines):
    def explode():
        raise ValueError("secret manuscript")

    try:
        try:
            explode()
        except ValueError as cause:
            raise RuntimeError("secret wrapper") from cause
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    (line,) = log_lines()
    assert line["log.logger"] == "uvicorn.error"
    assert line["error.type"] == "RuntimeError"
    assert "Caused by: ValueError" in line["error.stack_trace"]
    assert "\tat tests.test_logs.test_framework_exception" in line["error.stack_trace"]
    assert "secret" not in log_lines.text()


def test_warn_and_error_lines_without_an_exception_carry_the_call_site_stack(
    log_lines,
):
    def reject():
        logger = AppLogger.current()
        logger.warn("kafka event skipped", LogField.error_type("UNKNOWN_EVENT_TYPE"))

    reject()
    logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.")
    logging.getLogger("opentelemetry.exporter").error("Failed to export span batch")
    AppLogger.current().error("graph refresh failed")
    AppLogger.current().info("graph refresh run")

    ours, uvicorn, exporter, untyped, info = log_lines()
    stack = ours["error.stack_trace"].splitlines()
    assert stack[0] == "log site: UNKNOWN_EVENT_TYPE"
    # 가장 최근 프레임이 줄을 찍은 곳이고, 로깅 프레임은 빠진다
    logged_at = reject.__code__.co_firstlineno + 2
    assert stack[1] == (
        f"\tat {__name__}.{reject.__qualname__}(test_logs.py:{logged_at})"
    )
    assert stack[2].startswith(
        f"\tat {__name__}.test_warn_and_error_lines_without_an_exception_carry_"
    )
    assert 2 < len(stack) <= 31
    for frame in stack[1:]:
        # 모듈.함수(파일:줄)만 있다. 소스 코드 줄과 메시지는 없다
        assert re.fullmatch(r"\tat \S+\([^/()]+:\d+\)", frame)
        assert not frame.startswith(("\tat app.logs.", "\tat logging."))
    # 프레임워크 줄(uvicorn, OTLP exporter)은 logger 이름이 첫 줄이다
    for line, logger in (
        (uvicorn, "uvicorn.error"),
        (exporter, "opentelemetry.exporter"),
    ):
        framework = line["error.stack_trace"].splitlines()
        assert framework[0] == f"log site: {logger}"
        assert framework[1].startswith(f"\tat {__name__}.")
    # error.type이 없는 우리 줄은 logger 이름이 첫 줄이다
    assert untyped["error.stack_trace"].startswith(f"log site: {__name__}\n")
    assert "error.stack_trace" not in info
    assert "Invalid HTTP" not in json.dumps(uvicorn["error.stack_trace"])
    for line in (ours, uvicorn, exporter, untyped):
        assert "error.message" not in line


def test_first_value_wins_and_name_conflicts_are_dropped():
    root = {}
    put(root, "log.level", "INFO")
    put(root, "log.level", "DEBUG")
    put(root, "log", "flat")
    put(root, "error", "message")
    put(root, "error.type", "ignored")
    put(root, "missing", None)
    assert root == {"log": {"level": "INFO"}, "error": "message"}


def test_formatting_failure_still_writes_a_minimal_line():
    record = logging.LogRecord("lib", logging.INFO, __file__, 1, "%s %s", (1,), None)
    line = json.loads(EcsFormatter("graph-rag-api", None).format(record))
    assert line["message"] == "log formatting failed: TypeError"
    assert line["ecs"] == {"version": "8.11"}


def test_framework_line_in_a_flow_gets_trace_and_only_context_keys(log_lines):
    with LogFlow.kafka_record("topic", 1, 2, (), (), "sandbox record", GROUP) as flow:
        flow.add(LogField.project_id(PROJECT))
        logging.getLogger("librdkafka").warning("broker message")
    framework = log_lines.find("broker message")[0]
    ours = log_lines.find("sandbox record event processed")[0]
    assert framework["trace.id"] == ours["trace.id"]
    assert framework["loresentry.kafka.offset"] == 2
    # 도메인 ID 같은 나머지 문맥은 우리 줄에만 붙는다
    assert "loresentry.project_id" not in framework
    assert ours["loresentry.project_id"] == PROJECT


# ------------------------------------------------------------------ 필드


def test_param_and_result_values_are_converted_without_content():
    def entries(field):
        return dict(field.entries)

    assert entries(LogField.param("title", "비밀 원고")) == {
        "loresentry.param.title": "[redacted]",
        "loresentry.param.title_length": 5,
    }
    assert entries(LogField.param("flag", True)) == {"loresentry.param.flag": True}
    assert entries(LogField.param("count", 3)) == {"loresentry.param.count": 3}
    assert entries(LogField.result("color", Color.RED)) == {
        "loresentry.result.color": "RED"
    }
    assert entries(LogField.result("outcome", RefreshOutcome.NO_CHANGE)) == {
        "loresentry.result.outcome": "NO_CHANGE"
    }
    assert entries(LogField.param("id", UUID(PROJECT))) == {
        "loresentry.param.id": PROJECT
    }
    assert entries(LogField.param("ids", ("a", "b"))) == {
        "loresentry.param.ids_count": 2
    }
    assert entries(LogField.param("body", b"abc")) == {"loresentry.param.body_bytes": 3}
    assert entries(LogField.result("map", {"a": 1})) == {
        "loresentry.result.map_count": 1
    }
    assert LogField.param("missing", None) is None
    assert LogField.param("nan", float("nan")) is None
    assert LogField.param("object", object()) is None
    assert entries(LogField.code("kind", "A", {"A"})) == {"loresentry.param.kind": "A"}
    assert entries(LogField.result_code("kind", "free text", {"A"})) == {
        "loresentry.result.kind": "other"
    }


def test_ids_types_and_urls_keep_only_safe_shapes():
    assert dict(LogField.project_id(PROJECT.upper()).entries) == {
        "loresentry.project_id": PROJECT
    }
    assert LogField.cloudevent_id("not-a-uuid") is None
    assert LogField.file_id(None) is None
    assert dict(LogField.cloudevent_type("Secret", {"FileChanged"}).entries) == {
        "loresentry.cloudevent.type": "other"
    }
    assert dict(LogField.cloudevent_type("GraphRefreshCompleted").entries) == {
        "loresentry.cloudevent.type": "GraphRefreshCompleted"
    }
    url = LogField.url_full("https://user:pw@content-api:8443/projects/p?ids=secret#x")
    assert dict(url.entries) == {"url.full": "https://content-api:8443/projects/p"}
    assert dict(duration(timedelta(milliseconds=2)).entries) == {
        "event.duration": 2_000_000
    }


def test_safe_stack_trace_limits_frames_and_never_has_messages():
    def recurse(depth):
        if depth == 0:
            raise KeyError("secret key")
        recurse(depth - 1)

    try:
        recurse(40)
    except KeyError as failure:
        stack = dict(LogField.safe_stack_trace(failure).entries)["error.stack_trace"]
    lines = stack.splitlines()
    assert lines[0] == "KeyError"
    # 던진 곳부터 20개 프레임만 남긴다
    assert len(lines) == 21
    assert lines[1].startswith("\tat tests.test_logs.")
    assert "recurse(test_logs.py:" in lines[1]
    assert "secret" not in stack


# ------------------------------------------------------------------ 위치


def test_location_uses_enclosing_function_and_layer_rules(log_lines):
    emit = lambda: AppLogger.current().info("from lambda")  # noqa: E731
    emit()
    (line,) = log_lines()
    assert line["log.origin.function"] == (
        "test_location_uses_enclosing_function_and_layer_rules"
    )
    assert _layer("app.refresh.service", {}) == "service"
    assert _layer("app.kafka.handlers", {}) == "consumer"
    assert _layer("app.adapters.neptune_client", {}) == "client"
    assert _layer("app.refresh.outbox", {}) == "scheduler"
    assert _layer("app.adapters.refresh_inbox", {"LOG_LAYER": "repository"}) == (
        "repository"
    )
    assert _layer("app.refresh.rules", {}) == "other"


def test_flow_lines_are_located_at_the_function_that_opened_the_flow(log_lines):
    with LogFlow.task("media cleanup"):
        pass
    functions = {line["log.origin.function"] for line in log_lines()}
    assert functions == {
        "test_flow_lines_are_located_at_the_function_that_opened_the_flow"
    }


# ------------------------------------------------------------------ 흐름


def test_kafka_record_continues_the_header_trace_and_binds_record_context(
    log_lines, spans
):
    headers = (
        ("traceparent", INCOMING.encode()),
        ("ce_id", EVENT.encode()),
        ("ce_type", b"untrusted-secret"),
        ("ce_subject", b"not-a-uuid"),
    )
    # 다른 흐름의 span이 지금 span이어도 레코드의 부모는 헤더뿐이다
    with TRACER.start_as_current_span("other flow"):
        with LogFlow.kafka_record(
            "t", 5, 7, headers, {"Known"}, "file changed", GROUP
        ) as flow:
            flow.add(LogField.file_id(REQUEST))
            AppLogger.current().info("inside")
            # 이 흐름의 traceparent는 같은 trace의 consumer span이다
            stored = traceparent()
    received, inside, processed = log_lines()
    consumer = named(spans(), "t process")
    assert received["message"] == "file changed event received"
    assert processed["message"] == "file changed event processed"
    for line in (received, inside, processed):
        assert line_ids(line) == ids(consumer)
        assert line["loresentry.kafka.topic"] == "t"
        assert line["loresentry.kafka.partition"] == 5
        assert line["loresentry.cloudevent.id"] == EVENT
        assert line["loresentry.cloudevent.type"] == "other"
        assert "loresentry.cloudevent.subject" not in line
    assert inside["loresentry.file_id"] == processed["loresentry.file_id"] == REQUEST
    assert "loresentry.file_id" not in received
    assert processed["loresentry.result.outcome"] == "PROCESSED"
    assert processed["event.duration"] >= 0
    assert stored == f"00-{TRACE}-{ids(consumer)[1]}-01"
    assert "untrusted-secret" not in log_lines.text()
    assert AppLogger.current().trace_id is None
    # 헤더의 span 아래 CONSUMER span 하나와 messaging 속성
    assert consumer.kind is SpanKind.CONSUMER
    assert ids(consumer)[0] == TRACE
    assert consumer.parent.span_id == int(PARENT, 16) and consumer.parent.is_remote
    assert dict(consumer.attributes) == {
        "messaging.system": "kafka",
        "messaging.destination.name": "t",
        "messaging.consumer.group.name": GROUP,
        "messaging.destination.partition.id": "5",
        "messaging.kafka.offset": 7,
        "messaging.operation.type": "process",
    }
    assert consumer.status.status_code is StatusCode.UNSET


def test_kafka_record_without_header_starts_a_new_trace(log_lines, spans):
    with TRACER.start_as_current_span("other flow"):
        with LogFlow.kafka_record(
            "t", 0, 0, (("traceparent", b"bad"),), (), "x", GROUP
        ):
            pass
    received, processed = log_lines()
    consumer, other = spans()
    assert len(received["trace.id"]) == 32 and received["trace.id"] != TRACE
    assert processed["trace.id"] == received["trace.id"]
    # 지금 span(다른 흐름)의 자식이 아니라 새 trace의 root다
    assert consumer.parent is None
    assert ids(consumer)[0] != ids(other)[0]


def test_kafka_record_failure_is_warn_failed_and_reraised(log_lines, spans):
    with pytest.raises(RuntimeError):
        with LogFlow.kafka_record("t", 0, 0, (), (), "x", GROUP):
            raise RuntimeError("secret")
    processed = log_lines.find("x event processed")[0]
    assert processed["log.level"] == "WARN"
    assert processed["loresentry.result.outcome"] == "FAILED"
    assert processed["error.type"] == "RuntimeError"
    # 원인 줄과 겹쳐도 메시지 없는 스택을 붙인다
    assert processed["error.stack_trace"].startswith("RuntimeError\n")
    assert "secret" not in log_lines.text()
    (consumer,) = spans()
    assert (consumer.status.status_code, consumer.status.description) == (
        StatusCode.ERROR,
        "RuntimeError",
    )
    # 예외 event는 종류만 남긴다(메시지·스택 없음)
    assert [dict(event.attributes) for event in consumer.events] == [
        {"exception.type": "RuntimeError"}
    ]


def test_kafka_record_outcome_and_fail_set_the_end_line(log_lines, spans):
    with LogFlow.kafka_record("t", 0, 0, (), (), "x", GROUP) as flow:
        flow.outcome(Outcome.SKIPPED)
    with LogFlow.kafka_record("t", 0, 1, (), (), "x", GROUP) as flow:
        flow.outcome(Outcome.FAILED).fail("InvalidEvent")
    skipped, failed = log_lines.find("x event processed")
    assert (skipped["log.level"], skipped["loresentry.result.outcome"]) == (
        "INFO",
        "SKIPPED",
    )
    assert (failed["log.level"], failed["error.type"]) == ("WARN", "InvalidEvent")
    # 예외 없는 실패 줄에는 호출 위치 스택이 붙는다
    assert failed["error.stack_trace"].startswith("log site: InvalidEvent\n")
    ok, rejected = spans()
    assert ok.status.status_code is StatusCode.UNSET
    assert (rejected.status.status_code, rejected.status.description) == (
        StatusCode.ERROR,
        "InvalidEvent",
    )


def test_polling_logs_nothing_for_an_empty_round(log_lines):
    with LogFlow.polling("refresh-outbox-relay") as run:
        run.result(LogField.result("published", 0))
    assert log_lines() == []


def test_polling_round_with_lines_has_start_end_and_one_trace(log_lines):
    with LogFlow.polling("refresh-outbox-relay") as run:
        AppLogger.current().info("refresh outbox batch start")
        run.result(LogField.result("published", 1))
    start, batch, completed = log_lines()
    assert [start["message"], completed["message"]] == [
        "schedule run start",
        "schedule run completed",
    ]
    assert start["trace.id"] == batch["trace.id"] == completed["trace.id"]
    assert completed["loresentry.schedule.name"] == "refresh-outbox-relay"
    assert completed["loresentry.result.published"] == 1


def test_a_round_span_is_current_from_the_round_start_and_only_an_idle_one_is_marked(
    log_lines, spans
):
    with TRACER.start_as_current_span("other flow"):
        with LogFlow.polling("refresh-outbox-relay"):
            # 회차의 발행 대기 조회(Neptune 쿼리). 줄보다 먼저 돈다
            with TRACER.start_as_current_span("POST"):
                pass
    with LogFlow.polling("refresh-outbox-relay"):
        with TRACER.start_as_current_span("POST"):
            pass
        AppLogger.current().info("refresh outbox batch start")
    with LogFlow.polling("refresh-outbox-relay") as failed:
        failed.fail("DeliveryError")

    idle_query, idle, _other, busy_query, busy, failing = spans()
    start, batch, completed, failed_start, failed_end = log_lines()
    # 회차마다 새 root이고, 첫 줄 전의 조회도 그 자식이다
    assert {span.name for span in (idle, busy, failing)} == {"refresh-outbox-relay"}
    assert [span.parent for span in (idle, busy, failing)] == [None] * 3
    assert idle_query.parent.span_id == idle.context.span_id
    assert busy_query.parent.span_id == busy.context.span_id
    # 줄이 없던 회차만 표시가 붙는다. collector가 그 trace를 통째로 버린다
    assert idle.attributes.get("loresentry.polling.idle") is True
    assert "loresentry.polling.idle" not in busy.attributes
    assert "loresentry.polling.idle" not in failing.attributes
    assert (failing.status.status_code, failing.status.description) == (
        StatusCode.ERROR,
        "DeliveryError",
    )
    assert line_ids(start) == line_ids(batch) == line_ids(completed) == ids(busy)
    assert line_ids(failed_start) == line_ids(failed_end) == ids(failing)


def test_polling_and_schedule_swallow_exceptions_with_failed_lines(log_lines, spans):
    with LogFlow.polling("relay"):
        raise RuntimeError("secret")
    with LogFlow.schedule("purge", LogField.param("days", 3)):
        raise RuntimeError("secret")
    with LogFlow.schedule("purge"):
        pass
    assert log_lines.messages() == [
        "schedule run start",
        "schedule run failed",
        "schedule run start",
        "schedule run failed",
        "schedule run start",
        "schedule run completed",
    ]
    failed = log_lines.find("schedule run failed")[1]
    assert failed["log.level"] == "ERROR" and failed["error.type"] == "RuntimeError"
    assert "error.stack_trace" in failed
    assert log_lines.find("schedule run start")[1]["loresentry.param.days"] == 3
    assert "secret" not in log_lines.text()
    relay, purge_failed, purge = spans()
    assert [span.name for span in (relay, purge_failed, purge)] == [
        "relay",
        "purge",
        "purge",
    ]
    assert all(span.parent is None for span in (relay, purge_failed, purge))
    assert relay.status.status_code is purge_failed.status.status_code
    assert relay.status.status_code is StatusCode.ERROR
    assert purge.status.status_code is StatusCode.UNSET


def test_call_results_cover_success_upstream_errors_and_exceptions(log_lines, spans):
    with LogFlow.call("content", "http://content-api/projects/p?ids=x") as call:
        call.status(200)
    with LogFlow.call("content", "http://content-api/projects/p") as call:
        call.status(503)
    denied = ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "secret"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "GetObject",
    )
    with pytest.raises(ClientError):
        with LogFlow.call("s3"):
            raise denied
    completed, upstream, failed = log_lines()
    assert completed["message"] == "content call completed"
    assert completed["url.domain"] == "content-api"
    assert completed["url.full"] == "http://content-api/projects/p"
    assert completed["http.response.status_code"] == 200
    assert upstream["log.level"] == "WARN"
    assert upstream["error.type"] == "UPSTREAM_SERVER_ERROR"
    assert upstream["error.stack_trace"].startswith("log site: UPSTREAM_SERVER_ERROR\n")
    assert failed["message"] == "s3 call failed"
    assert failed["error.type"] == "ClientError"
    assert failed["error.code"] == "AccessDenied"
    assert failed["http.response.status_code"] == 403
    assert failed["error.stack_trace"].startswith("botocore.exceptions.ClientError\n")
    assert "secret" not in log_lines.text()
    ok, five, raised = spans()
    assert [span.name for span in (ok, five, raised)] == [
        "content call",
        "content call",
        "s3 call",
    ]
    for span, line in zip(
        (ok, five, raised), (completed, upstream, failed), strict=True
    ):
        # 결과 줄은 그 호출 span 안에서 찍힌다
        assert line_ids(line) == ids(span)
        assert span.kind is SpanKind.INTERNAL
    assert ok.status.status_code is StatusCode.UNSET
    assert (five.status.status_code, five.status.description) == (
        StatusCode.ERROR,
        "UPSTREAM_SERVER_ERROR",
    )
    assert (raised.status.status_code, raised.status.description) == (
        StatusCode.ERROR,
        "ClientError",
    )
    assert [dict(event.attributes) for event in raised.events] == [
        {"exception.type": "botocore.exceptions.ClientError"}
    ]


def test_a_call_span_wraps_the_instrumented_client_span(log_lines, spans):
    with LogFlow.task("graph refresh"):
        with LogFlow.call("content", "http://content-api/x") as call:
            # httpx 계측이 그 안에서 여는 CLIENT span
            with TRACER.start_as_current_span("GET", kind=SpanKind.CLIENT):
                pass
            call.status(200)
        # 계측이 없는 대상(OpenAI Responses API): 우리 span이 그 CLIENT span이다
        with LogFlow.call("openai", server="api.openai.com", model="gpt-6-luna"):
            pass
    http, content, openai, task = spans()
    _start, content_line, openai_line, _end = log_lines()
    assert http.parent.span_id == content.context.span_id
    assert content.parent.span_id == openai.parent.span_id == task.context.span_id
    assert line_ids(content_line) == ids(content)
    assert line_ids(openai_line) == ids(openai)
    assert (openai.name, openai.kind) == ("openai call", SpanKind.CLIENT)
    assert dict(openai.attributes) == {
        "server.address": "api.openai.com",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-6-luna",
    }


def test_task_lines_and_spans(log_lines, spans):
    with LogFlow.task("graph refresh", LogField.kafka_topic("t")) as task:
        task.result(LogField.result("rows", 2))
    with pytest.raises(KeyError):
        with LogFlow.task("graph refresh"):
            raise KeyError("secret")
    with LogFlow.task("graph refresh") as task:
        task.fail("MODEL_TIMEOUT")
    start, completed, _, raised, _, failed = log_lines()
    assert start["loresentry.kafka.topic"] == "t"
    assert completed["loresentry.result.rows"] == 2
    assert (raised["log.level"], raised["error.type"]) == ("ERROR", "KeyError")
    assert raised["error.stack_trace"].startswith("KeyError\n")
    assert (failed["log.level"], failed["error.type"]) == ("WARN", "MODEL_TIMEOUT")
    assert failed["error.stack_trace"].startswith("log site: MODEL_TIMEOUT\n")
    done, error, refused = spans()
    assert [span.name for span in (done, error, refused)] == ["graph refresh"] * 3
    # 흐름 밖에서 시작한 작업은 root이고, 그 줄은 작업 span 안에서 찍힌다
    assert all(span.parent is None for span in (done, error, refused))
    assert line_ids(start) == line_ids(completed) == ids(done)
    assert error.status.status_code is refused.status.status_code
    assert refused.status.description == "MODEL_TIMEOUT"


def test_a_worker_loop_has_task_lines_but_no_span(log_lines, spans):
    with LogFlow.worker("kafka worker", LogField.kafka_topic("t")):
        # 루프 안의 레코드는 저마다 자기 span을 연다
        with LogFlow.kafka_record("t", 0, 0, (), (), "x", GROUP):
            pass
    with pytest.raises(KeyError):
        with LogFlow.worker("kafka worker"):
            raise KeyError("secret")
    assert log_lines.messages() == [
        "kafka worker start",
        "x event received",
        "x event processed",
        "kafka worker completed",
        "kafka worker start",
        "kafka worker failed",
    ]
    (record,) = spans()
    assert record.name == "t process"
    start, received, _, completed, _, failed = log_lines()
    assert "trace.id" not in start and "trace.id" not in completed
    assert line_ids(received) == ids(record)
    assert (failed["log.level"], failed["error.type"]) == ("ERROR", "KeyError")


def test_a_line_carries_the_span_current_when_it_is_written(log_lines, spans):
    with LogFlow.task("graph refresh"):
        AppLogger.current().info("graph refresh step")
        # 계측이 작업 안에서 여는 span(Neptune 쿼리)
        with TRACER.start_as_current_span("POST"):
            AppLogger.current().info("refresh inbox find")
            logging.getLogger("httpx").warning("framework line")
    with TRACER.start_as_current_span("outside"):
        AppLogger.current().info("graph refresh outside")
    AppLogger.current().info("graph refresh detached")

    start, step, query_line, framework, end, outside, detached = log_lines()
    found = spans()
    task, query = named(found, "graph refresh"), named(found, "POST")
    assert line_ids(start) == line_ids(step) == line_ids(end) == ids(task)
    assert line_ids(query_line) == line_ids(framework) == ids(query)
    assert query.parent.span_id == task.context.span_id
    assert line_ids(outside) == ids(named(found, "outside"))
    assert line_ids(detached) == (None, None)


def test_a_line_keeps_the_flow_ids_when_the_current_span_is_missing_or_elsewhere(
    log_lines, spans
):
    with LogScope.stored(INCOMING, "graph refresh job"):
        # 다른 trace의 span이나, span이 없는 문맥(문맥이 건너가지 않은 스레드)
        with TRACER.start_as_current_span("elsewhere", context=Context()):
            AppLogger.current().info("graph refresh other trace")
        token = otel_context.attach(Context())
        try:
            AppLogger.current().info("graph refresh no span")
            logging.getLogger("httpx").warning("framework line")
        finally:
            otel_context.detach(token)

    job = named(spans(), "graph refresh job")
    found = log_lines()
    assert len(found) == 3
    for line in found:
        assert line_ids(line) == ids(job)


def test_an_outbox_scope_opens_a_span_under_the_stored_trace(log_lines, spans):
    with LogFlow.polling("refresh-outbox-relay"):
        AppLogger.current().info("refresh outbox batch start")
        with LogScope.outbox_event(
            INCOMING,
            "refresh-outbox-relay",
            EVENT,
            "GraphRefreshCompleted",
            REQUEST,
            LogField.project_id(PROJECT),
        ):
            AppLogger.current().info("graph refresh completion publish")
        # 저장된 값이 없거나 틀린 행은 그 행만 새 trace다
        with LogScope.outbox_event(
            None, "refresh-outbox-relay", EVENT, "GraphRefreshCompleted", REQUEST
        ):
            AppLogger.current().info("graph refresh completion publish")
        with pytest.raises(RuntimeError):
            with LogScope.outbox_event(
                "00-wrong", "refresh-outbox-relay", EVENT, "GraphRefreshCompleted", None
            ):
                raise RuntimeError("secret")

    stored, fresh, broken, relay = spans()
    _start, _batch, published, unstored, completed = log_lines()
    # 행에 저장한 span이 부모이고, trace가 다른 회차는 link로 이어진다
    assert (stored.name, stored.kind) == (
        "outbox GraphRefreshCompleted",
        SpanKind.INTERNAL,
    )
    assert ids(stored)[0] == TRACE
    assert stored.parent.span_id == int(PARENT, 16)
    assert [link.context.span_id for link in stored.links] == [relay.context.span_id]
    assert line_ids(published) == ids(stored)
    assert published["loresentry.schedule.name"] == "refresh-outbox-relay"
    assert published["loresentry.cloudevent.type"] == "GraphRefreshCompleted"
    assert published["loresentry.cloudevent.subject"] == REQUEST
    assert published["loresentry.project_id"] == PROJECT
    for span in (fresh, broken):
        assert span.parent is None
        assert ids(span)[0] not in (TRACE, ids(relay)[0])
        assert [link.context.span_id for link in span.links] == [relay.context.span_id]
    assert line_ids(unstored) == ids(fresh)
    assert broken.status.status_code is StatusCode.ERROR
    # 범위를 닫으면 회차로 돌아간다
    assert line_ids(completed) == ids(relay)


# ------------------------------------------------------------------ 설정


def test_configuration_is_idempotent_and_quiets_framework_loggers():
    configure_logging("graph-rag-api")
    configure_logging("graph-rag-api")
    ours = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler.formatter, EcsFormatter)
    ]
    assert len(ours) == 1
    assert (
        logging.getLogger("uvicorn").propagate
        and not logging.getLogger("uvicorn").handlers
    )
    assert logging.getLogger("uvicorn.access").disabled
    assert logging.getLogger("httpx").level == logging.WARNING
    assert sys.excepthook.__module__ == "app.logs._format"
    assert consumer_config("broker", "group")["logger"].name == "librdkafka"
    assert producer_config("broker")["logger"].name == "librdkafka"


def test_uncaught_thread_exception_is_a_log_line_without_message(log_lines):
    try:
        raise RuntimeError("secret thread failure")
    except RuntimeError as failure:
        _uncaught_in_thread(
            SimpleNamespace(
                exc_type=RuntimeError,
                exc_value=failure,
                exc_traceback=failure.__traceback__,
                thread=threading.current_thread(),
            )
        )
    (line,) = log_lines()
    assert line["message"] == "uncaught exception in thread"
    assert line["error.type"] == "RuntimeError"
    assert "secret" not in log_lines.text()


# ------------------------------------------------------------------ HTTP 요청과 전파


def propagation_app(content_base):
    """받은 요청을 처리하며 content를 부르는 작은 앱. 운영 앱과 같은 계측·미들웨어 순서다."""
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)
    setup_tracing(app)

    @app.get("/projects/{project_id}")
    def read(project_id: str):
        AppLogger.current().info("project read requested")
        with ContentDocumentSource(content_base, user_id=USER) as source:
            source.fetch_documents(project_id, (REQUEST,))
        return {"trace_id": AppLogger.current().trace_id}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/broken")
    def broken():
        raise RuntimeError("secret bug")

    return app


def test_incoming_traceparent_reaches_lines_and_the_outgoing_content_call(
    log_lines, spans, loopback
):
    loopback.respond = lambda request: {
        "project_id": PROJECT,
        "documents": [],
        "missing": [REQUEST],
    }
    with TestClient(propagation_app(loopback.base)) as client:
        response = client.get(
            f"/projects/{PROJECT}?q=secret",
            headers={"traceparent": INCOMING, "X-User-Id": USER},
        )
    assert response.json() == {"trace_id": TRACE}
    # TestClient 자신의 httpx 줄(프레임워크 줄)은 빼고 본다
    lines = [line for line in log_lines() if line.get("log.package")]
    assert [line["message"] for line in lines] == [
        "request received",
        "project read requested",
        "content documents get call",
        "content call completed",
        "request completed",
    ]
    for line in lines:
        assert line["trace.id"] == TRACE
        assert line["user.id"] == USER
        assert line["url.path"] == f"/projects/{PROJECT}"
        assert line["http.route"] == "/projects/{project_id}"
        assert line["http.request.method"] == "GET"
    received, completed = lines[0], lines[-1]
    assert (received["log.layer"], received["log.logger"]) == (
        "filter",
        "app.logs._request",
    )
    assert completed["http.response.status_code"] == 200
    assert completed["event.duration"] > 0
    found = spans()
    server = named(found, "GET /projects/{project_id}")
    call = named(found, "content call")
    (http,) = [span for span in found if span.kind is SpanKind.CLIENT]
    # 서버 span(받은 traceparent를 잇는다) → content call → httpx CLIENT span. 내부 ASGI span은 없다
    assert server.kind is SpanKind.SERVER and server.parent.span_id == int(PARENT, 16)
    assert call.parent.span_id == server.context.span_id
    assert http.parent.span_id == call.context.span_id
    assert not [span for span in found if span.name.endswith((" receive", " send"))]
    # 요청 줄은 서버 span, 호출 결과 줄은 호출 span 안에서 찍힌다
    assert line_ids(received) == line_ids(completed) == ids(server)
    assert line_ids(lines[3]) == ids(call)
    # 나가는 content 호출이 httpx CLIENT span의 traceparent를 싣는다. span url.full의 쿼리(문서 ID)는 collector가 지운다
    (request,) = loopback.received
    assert traceparent_ids(request.headers["traceparent"]) == ids(http)
    assert "secret" not in log_lines.text()


def test_health_404_and_unhandled_errors(log_lines, spans):
    with TestClient(
        propagation_app("http://content-api"), raise_server_exceptions=False
    ) as client:
        client.get("/health")
        client.get("/missing")
        assert client.get("/broken").status_code == 500
    health = [line for line in log_lines() if line.get("url.path") == "/health"]
    assert [line["message"] for line in health] == [
        "request received",
        "request completed",
    ]
    assert all(line["tags"] == ["health"] for line in health)
    missing = log_lines.find("request completed")[1]
    assert missing["log.level"] == "WARN"
    assert missing["http.response.status_code"] == 404
    assert "http.route" not in missing
    (failed,) = log_lines.find("request failed with an unhandled exception")
    assert failed["log.level"] == "ERROR"
    assert failed["error.type"] == "RuntimeError"
    assert failed["http.response.status_code"] == 500
    assert "secret bug" not in log_lines.text()
    # 헬스체크도 서버 span을 남긴다(성공한 것은 collector가 버린다)
    assert named(spans(), "GET /health").kind is SpanKind.SERVER


# ------------------------------------------------------------------ Kafka 헤더 → 완료 헤더


def test_record_trace_is_stored_with_the_completion_and_sent_by_the_relay(
    log_lines, spans, loopback
):
    """Kafka 헤더 T → consumer span → InboxEvent traceparent → relay 행의 span → 완료 헤더 T."""
    request = RefreshRequest(
        JobKey(PROJECT, REQUEST, "a" * 64), ArtifactRef("input", "refresh.json"), ()
    )
    completion = Completion(
        request.job, ArtifactRef("result", "refresh.json"), RefreshOutcome.NO_CHANGE
    )
    queries = []

    def neptune(sent):
        query = json.loads(sent.body)["gremlin"]
        queries.append(query)
        data = ["APPLIED"]
        if "outbox_pending','true')" in query and ".project(" in query:
            # 발행 대기 조회: 기록한 traceparent를 그대로 돌려준다
            data = [
                {
                    "identifier": f"inbox:graph-rag-refresh:{PROJECT}:{REQUEST}",
                    "receipt": receipt_json(completion),
                    "created_at": "2026-11-04T00:00:00+00:00",
                    "traceparent": query_traceparent(queries[0]),
                }
            ]
        if ".property(single,'outbox_pending','false')" in query:
            data = ["SENT"]
        return {"status": {"code": 200}, "result": {"data": data}}

    loopback.respond = neptune
    with NeptuneRefreshInbox(loopback.base) as inbox:
        headers = (("traceparent", INCOMING.encode()),)
        with LogFlow.kafka_record(
            "t", 0, 0, headers, (), "graph refresh requested", GROUP
        ):
            inbox.complete(request, completion)
        stored = query_traceparent(queries[0])
        producer = ProducerStub()
        relay = RefreshOutboxRelay(
            inbox, KafkaPublisher(producer, timeout=1, clock=lambda: producer.now)
        )
        # relay.run의 회차 하나와 같다
        with LogFlow.polling("refresh-outbox-relay"):
            assert relay.drain(threading.Event()) == 1

    found = spans()
    consumer = named(found, "t process")
    row = named(found, "outbox GraphRefreshCompleted")
    publish = named(found, f"{REFRESH_COMPLETED} publish")
    relay_round = named(found, "refresh-outbox-relay")
    write, pending, mark_sent = [span for span in found if span.name == "POST"]
    # InboxEvent에 저장한 값은 그 레코드의 consumer span이다
    assert stored == f"00-{TRACE}-{ids(consumer)[1]}-01"
    assert write.parent.span_id == consumer.context.span_id
    # 회차 trace에는 회차 span과 발행 대기 조회만 남는다
    assert relay_round.parent is None and ids(relay_round)[0] != TRACE
    assert pending.parent.span_id == relay_round.context.span_id
    # 행의 span은 저장된 span의 자식이고(회차로 link), 발행 span과 발행 완료 기록 쿼리가 그 아래에 있다
    assert row.parent.span_id == consumer.context.span_id
    assert [link.context.span_id for link in row.links] == [relay_round.context.span_id]
    assert (publish.kind, publish.parent.span_id) == (
        SpanKind.PRODUCER,
        row.context.span_id,
    )
    assert dict(publish.attributes) == {
        "messaging.system": "kafka",
        "messaging.destination.name": REFRESH_COMPLETED,
        "messaging.operation.type": "send",
    }
    assert mark_sent.parent.span_id == row.context.span_id
    # 완료 헤더는 저장된 trace를 잇는 발행 span이다
    ((topic, sent),) = producer.outgoing
    assert topic == REFRESH_COMPLETED
    assert header_ids(sent["headers"]) == ids(publish)
    assert ids(publish)[0] == TRACE
    # 행마다의 줄(발행, 발행 완료 기록)은 저장한 trace로 남는다
    for message in (
        "graph refresh completion publish",
        "kafka call completed",
        "refresh outbox mark sent",
    ):
        (line,) = log_lines.find(message)
        assert line["trace.id"] == TRACE
        assert line["loresentry.schedule.name"] == "refresh-outbox-relay"
        assert line["loresentry.cloudevent.type"] == "GraphRefreshCompleted"
        assert line["loresentry.refresh.request_id"] == REQUEST
    published = log_lines.find("kafka call completed")[0]
    assert line_ids(published) == ids(publish)
    assert published["loresentry.kafka.topic"] == REFRESH_COMPLETED
    assert published["loresentry.kafka.offset"] == 41
    assert line_ids(log_lines.find("refresh outbox mark sent")[0]) == ids(row)


def test_a_row_without_a_stored_trace_is_published_in_its_own_trace(spans):
    producer = ProducerStub()
    completion = Completion(
        JobKey(PROJECT, REQUEST, "a" * 64),
        ArtifactRef("result", "refresh.json"),
        RefreshOutcome.NO_CHANGE,
    )
    publisher = KafkaPublisher(producer, timeout=1, clock=lambda: producer.now)
    with LogFlow.polling("refresh-outbox-relay"):
        with LogScope.outbox_event(
            None, "refresh-outbox-relay", EVENT, "GraphRefreshCompleted", REQUEST
        ):
            publisher.publish(completion)
    publish, row, relay_round = spans()
    ((_, sent),) = producer.outgoing
    # 회차 trace가 아니라 그 행만의 새 trace다
    assert row.parent is None
    assert ids(row)[0] != ids(relay_round)[0]
    assert header_ids(sent["headers"]) == ids(publish)
    assert ids(publish)[0] == ids(row)[0]


def test_a_dead_lettered_record_continues_the_record_trace(log_lines, spans):
    producer = ProducerStub()
    publisher = KafkaPublisher(producer, timeout=1, clock=lambda: producer.now)
    original = content_record()
    record = replace(
        original,
        value=b"not JSON",
        headers=original.headers + (("traceparent", INCOMING.encode()),),
    )
    consume = BusinessConsumerHandler(
        REFRESH_REQUESTED,
        {"GraphRefreshRequested": lambda event: pytest.fail("reached business")},
        publisher,
        threading.Event(),
    )
    consume(record)

    found = spans()
    consumer = named(found, f"{REFRESH_REQUESTED} process")
    publish = named(found, f"{REFRESH_REQUESTED}.graph-rag.dlq publish")
    ((topic, sent),) = producer.outgoing
    assert topic == f"{REFRESH_REQUESTED}.graph-rag.dlq"
    # DLQ 레코드의 traceparent는 하나이고, 원본 trace를 잇는 발행 span(그 레코드의 consumer span 아래)이다
    assert header_ids(sent["headers"]) == ids(publish)
    assert publish.parent.span_id == consumer.context.span_id
    assert ids(consumer)[0] == TRACE
    assert consumer.attributes["messaging.consumer.group.name"] == GROUP
    # 다른 원본 헤더는 순서 그대로 남는다
    names = [name for name, _ in sent["headers"]]
    assert names[: len(original.headers)] == [name for name, _ in original.headers]
    assert names[-1] == "traceparent"
    assert "kafka_dlt-original-topic" in names
    (dead,) = log_lines.find("kafka event dead-lettered")
    assert line_ids(dead) == ids(consumer)
    assert dead["error.stack_trace"].startswith("app.kafka.errors.InvalidEvent\n")
    assert (consumer.status.status_code, consumer.status.description) == (
        StatusCode.ERROR,
        "InvalidEvent",
    )


def query_traceparent(query: str) -> str | None:
    """InboxEvent 쓰기 쿼리에 실린 traceparent 속성 값."""
    marker = ".property(single,'traceparent','"
    if marker not in query:
        return None
    return query.split(marker, 1)[1].split("'", 1)[0]


def receipt_json(completion: Completion) -> str:
    from app.adapters.refresh_inbox import Receipt

    return Receipt(
        schema_version="refresh-inbox-v1", completion=completion
    ).model_dump_json()


# ------------------------------------------------------------------ Neptune IAM 서명과 계측


def test_neptune_iam_signature_holds_with_the_traceparent_the_instrumentation_adds(
    spans, loopback
):
    """서명은 send 전에 하고 httpx 계측은 transport에서 traceparent를 더한다. 더한 헤더는 SignedHeaders에 없고 서명이 그대로 맞다."""
    loopback.respond = lambda request: {
        "status": {"code": 200},
        "result": {"data": [1]},
    }
    credentials = Credentials("AKIDEXAMPLE", "example-secret", "example-session")
    session = SimpleNamespace(get_credentials=lambda: credentials)
    with NeptuneGremlinClient(
        loopback.base, iam_auth=True, region="ap-northeast-2", session=session
    ) as client:
        with LogFlow.task("graph projection"):
            assert client.execute("g.V().count()") == [1]

    (request,) = loopback.received
    headers = request.headers
    found = spans()
    task = named(found, "graph projection")
    (http,) = [span for span in found if span.kind is SpanKind.CLIENT]
    # 계측이 그 쿼리의 CLIENT span을 열고 traceparent를 더했다. 쿼리 본문은 span에 없다
    assert http.parent.span_id == task.context.span_id
    assert traceparent_ids(headers["traceparent"]) == ids(http)
    assert http.attributes["url.full"] == f"{loopback.base}/gremlin"
    assert "g.V()" not in json.dumps({**http.attributes})
    # 서명한 헤더 목록에 traceparent가 없다
    matched = re.fullmatch(
        r"AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/\d{8}/ap-northeast-2/neptune-db/"
        r"aws4_request, SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})",
        headers["authorization"],
    )
    assert matched is not None
    signed = matched.group(1).split(";")
    assert "traceparent" not in signed
    assert {"host", "x-amz-date", "x-amz-security-token"} <= set(signed)
    # 받은 요청에서 SignedHeaders의 헤더만으로 서명을 다시 계산하면(Neptune이 하는 검사) 같다
    received = AWSRequest(
        method=request.method,
        url=f"{loopback.base}{request.path}",
        data=request.body,
        headers={name: headers[name] for name in signed},
    )
    received.context["timestamp"] = headers["x-amz-date"]
    signer = SigV4Auth(
        credentials.get_frozen_credentials(), "neptune-db", "ap-northeast-2"
    )
    canonical = signer.canonical_request(received)
    expected = signer.signature(signer.string_to_sign(received, canonical), received)
    assert expected == matched.group(2)


# ------------------------------------------------------------------ 추적 엔진(이미지 시작 스크립트)

# 이미지의 시작 스크립트와 실제 distro로 띄운다: 어느 호출에 traceparent가 실리는지, 무엇이 켜졌는지
DISTRO_CHECK = """
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import (
    HTTPX2ClientInstrumentor,
    HTTPXClientInstrumentor,
)
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

seen = {}


class Handler(BaseHTTPRequestHandler):
    def answer(self):
        seen[self.path] = [name.lower() for name in self.headers]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = answer

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"

with trace.get_tracer("check").start_as_current_span("request"):
    with httpx.Client(base_url=base) as content:
        content.get("/projects/files/content")
    client = OpenAI(api_key="test", base_url=base + "/v1", max_retries=0)
    try:
        client.responses.create(model="test", input="test")
    except Exception:
        pass
    client.close()

provider = trace.get_tracer_provider()
instrumentors = {
    "httpx": HTTPXClientInstrumentor,
    "httpx2": HTTPX2ClientInstrumentor,
    "botocore": BotocoreInstrumentor,
    "fastapi": FastAPIInstrumentor,
    "system_metrics": SystemMetricsInstrumentor,
}
print(json.dumps({
    "content": seen["/projects/files/content"],
    "openai": seen["/v1/responses"],
    "sampler": provider.sampler.get_description(),
    "resource": {
        name: provider.resource.attributes.get(name)
        for name in ("service.name", "service.version", "deployment.environment.name")
    },
    "detectors": sorted(
        name for name in provider.resource.attributes if name.startswith(("cloud.", "k8s.cluster"))
    ),
    "instrumented": {
        name: instrumentor().is_instrumented_by_opentelemetry
        for name, instrumentor in instrumentors.items()
    },
}))
"""


def entrypoint_env(path: str, **values: str) -> dict[str, str]:
    """이미지의 환경: 이 셸의 OTEL_* 없이, 이미지 태그를 APP_VERSION으로."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OTEL_", "ELASTIC_OTEL_"))
    }
    env.update(PATH=path, APP_VERSION="build-9-1", **values)
    return env


def test_the_entrypoint_sets_safe_defaults_and_keeps_what_the_overlay_sets(tmp_path):
    # opentelemetry-instrument 대역. 프로세스가 받을 환경과 명령을 출력한다
    stubs = tmp_path / "bin"
    stubs.mkdir()
    stub = stubs / "opentelemetry-instrument"
    stub.write_text('#!/bin/sh\nenv | grep "^OTEL_"\necho "COMMAND=$*"\n')
    stub.chmod(0o755)
    path = f"{stubs}:{os.environ['PATH']}"

    def run(*command: str, **values: str) -> list[str]:
        done = subprocess.run(
            ["bash", str(ENTRYPOINT), *command],
            env=entrypoint_env(path, **values),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stderr == ""
        return done.stdout.splitlines()

    api = run("uvicorn", "app.main:app", "--no-access-log")
    dev = run(
        "uvicorn",
        "app.main:app",
        OTEL_TRACES_EXPORTER="otlp",
        OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=dev",
        OTEL_EXPERIMENTAL_RESOURCE_DETECTORS="otel",
    )

    assert "COMMAND=uvicorn app.main:app --no-access-log" in api
    assert {
        "OTEL_SERVICE_NAME=graph-rag-api",
        "OTEL_TRACES_EXPORTER=none",
        "OTEL_METRICS_EXPORTER=none",
        "OTEL_LOGS_EXPORTER=none",
        "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",
        "OTEL_TRACES_SAMPLER=parentbased_always_on",
        "OTEL_PROPAGATORS=tracecontext",
        "OTEL_SEMCONV_STABILITY_OPT_IN=http,database",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false",
        # system_metrics는 켠다(런타임 지표). 지표를 보낼지는 OTEL_METRICS_EXPORTER가 정한다
        "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=fastapi,httpx2,logging,confluent_kafka",
        # 클라우드 detector(aws_eks의 403 평문 줄) 없이, otel이 마지막이다
        "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS="
        "process_runtime,os,telemetry_distro,service_instance,containerid,otel",
        "OTEL_RESOURCE_ATTRIBUTES=service.version=build-9-1",
    } <= set(api)
    # overlay가 준 값이 먼저이고, 이미지 태그는 그 resource 앞에 붙는다
    assert "OTEL_TRACES_EXPORTER=otlp" in dev
    assert "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS=otel" in dev
    assert (
        "OTEL_RESOURCE_ATTRIBUTES="
        "service.version=build-9-1,deployment.environment.name=dev"
    ) in dev


def test_under_the_distro_only_content_calls_carry_a_traceparent():
    done = subprocess.run(
        ["bash", str(ENTRYPOINT), sys.executable, "-c", DISTRO_CHECK],
        env=entrypoint_env(
            f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=dev",
            # Kubernetes 안처럼 둔다. distro는 이때 클라우드 detector(aws_eks 등)를 더하려 한다
            KUBERNETES_SERVICE_HOST="127.0.0.1",
        ),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    found = json.loads(done.stdout.splitlines()[-1])
    # content(httpx)에는 traceparent만 실리고, OpenAI(httpx2)에는 아무것도 실리지 않는다
    assert "traceparent" in found["content"]
    assert not {"traceparent", "tracestate", "baggage"} & set(found["openai"])
    assert "tracestate" not in found["content"]
    assert found["sampler"].startswith("ParentBased{root:AlwaysOnSampler")
    assert found["resource"] == {
        "service.name": "graph-rag-api",
        "service.version": "build-9-1",
        "deployment.environment.name": "dev",
    }
    assert found["detectors"] == []
    assert found["instrumented"] == {
        "httpx": True,
        "httpx2": False,
        "botocore": True,
        "fastapi": False,
        "system_metrics": True,
    }
    # distro 자신은 평문 줄을 남기지 않는다
    assert done.stderr == ""
