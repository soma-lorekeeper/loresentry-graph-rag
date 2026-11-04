"""app/logs의 출력 형식·필드 변환·흐름 줄·trace 전파를 확인한다(LOG_SCHEMA.md 5절, 7절)."""

import json
import logging
import sys
import threading
from datetime import timedelta
from enum import Enum
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.adapters.content_documents import ContentDocumentSource
from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.kafka.messages import REFRESH_COMPLETED
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
from tests.test_kafka_publisher import ProducerStub

TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"
INCOMING = f"00-{TRACE}-{PARENT}-01"
PROJECT = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"
EVENT = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"


class Color(Enum):
    RED = "red"


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
    with LogFlow.kafka_record("topic", 1, 2, (), (), "sandbox record") as flow:
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


def test_kafka_record_continues_the_header_trace_and_binds_record_context(log_lines):
    headers = (
        ("traceparent", INCOMING.encode()),
        ("ce_id", EVENT.encode()),
        ("ce_type", b"untrusted-secret"),
        ("ce_subject", b"not-a-uuid"),
    )
    with LogFlow.kafka_record("t", 5, 7, headers, {"Known"}, "file changed") as flow:
        flow.add(LogField.file_id(REQUEST))
        AppLogger.current().info("inside")
        # 이 흐름의 traceparent는 같은 trace의 consumer span이다
        stored = traceparent()
    received, inside, processed = log_lines()
    assert received["message"] == "file changed event received"
    assert processed["message"] == "file changed event processed"
    for line in (received, inside, processed):
        assert line["trace.id"] == TRACE
        assert line["span.id"] != PARENT
        assert line["loresentry.kafka.topic"] == "t"
        assert line["loresentry.kafka.partition"] == 5
        assert line["loresentry.cloudevent.id"] == EVENT
        assert line["loresentry.cloudevent.type"] == "other"
        assert "loresentry.cloudevent.subject" not in line
    assert inside["loresentry.file_id"] == processed["loresentry.file_id"] == REQUEST
    assert "loresentry.file_id" not in received
    assert processed["loresentry.result.outcome"] == "PROCESSED"
    assert processed["event.duration"] >= 0
    assert stored.startswith(f"00-{TRACE}-") and stored.endswith("-01")
    assert "untrusted-secret" not in log_lines.text()
    assert AppLogger.current().trace_id is None


def test_kafka_record_without_header_starts_a_new_trace(log_lines):
    with LogFlow.kafka_record("t", 0, 0, (("traceparent", b"bad"),), (), "x"):
        pass
    received, processed = log_lines()
    assert len(received["trace.id"]) == 32 and received["trace.id"] != TRACE
    assert processed["trace.id"] == received["trace.id"]


def test_kafka_record_failure_is_warn_failed_and_reraised(log_lines):
    with pytest.raises(RuntimeError):
        with LogFlow.kafka_record("t", 0, 0, (), (), "x"):
            raise RuntimeError("secret")
    processed = log_lines.find("x event processed")[0]
    assert processed["log.level"] == "WARN"
    assert processed["loresentry.result.outcome"] == "FAILED"
    assert processed["error.type"] == "RuntimeError"
    assert "error.stack_trace" not in processed


def test_kafka_record_outcome_and_fail_set_the_end_line(log_lines):
    with LogFlow.kafka_record("t", 0, 0, (), (), "x") as flow:
        flow.outcome(Outcome.SKIPPED)
    with LogFlow.kafka_record("t", 0, 1, (), (), "x") as flow:
        flow.outcome(Outcome.FAILED).fail("InvalidEvent")
    skipped, failed = log_lines.find("x event processed")
    assert (skipped["log.level"], skipped["loresentry.result.outcome"]) == (
        "INFO",
        "SKIPPED",
    )
    assert (failed["log.level"], failed["error.type"]) == ("WARN", "InvalidEvent")


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


def test_polling_and_schedule_swallow_exceptions_with_failed_lines(log_lines):
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


def test_call_results_cover_success_upstream_errors_and_exceptions(log_lines):
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
    assert failed["message"] == "s3 call failed"
    assert failed["error.type"] == "ClientError"
    assert failed["error.code"] == "AccessDenied"
    assert failed["http.response.status_code"] == 403
    assert "secret" not in log_lines.text()


def test_task_lines_and_scope_binding(log_lines):
    with LogFlow.task("kafka worker", LogField.kafka_topic("t")) as task:
        task.result(LogField.result("rows", 2))
    with pytest.raises(KeyError):
        with LogFlow.task("kafka worker"):
            raise KeyError("secret")
    with LogScope.outbox_event(
        INCOMING,
        "refresh-outbox-relay",
        EVENT,
        "GraphRefreshCompleted",
        REQUEST,
        LogField.project_id(PROJECT),
    ):
        AppLogger.current().info("graph refresh completion publish")
    start, completed, _, failed, published = log_lines()
    assert start["loresentry.kafka.topic"] == "t"
    assert completed["loresentry.result.rows"] == 2
    assert (failed["log.level"], failed["error.type"]) == ("ERROR", "KeyError")
    # outbox 행의 줄은 그 행에 저장한 trace로 남는다
    assert (published["trace.id"], published["span.id"]) == (TRACE, PARENT)
    assert published["loresentry.cloudevent.type"] == "GraphRefreshCompleted"
    assert published["loresentry.cloudevent.subject"] == REQUEST
    assert published["loresentry.project_id"] == PROJECT


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


def propagation_app(outgoing):
    """받은 요청을 처리하며 content를 부르는 작은 앱. 운영 앱과 같은 계측·미들웨어 순서다."""
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)
    FastAPIInstrumentor.instrument_app(app)

    def content(request):
        outgoing.append(request)
        return httpx.Response(
            200,
            json={"project_id": PROJECT, "documents": [], "missing": [REQUEST]},
        )

    @app.get("/projects/{project_id}")
    def read(project_id: str):
        AppLogger.current().info("project read requested")
        with httpx.Client(transport=httpx.MockTransport(content)) as client:
            source = ContentDocumentSource(
                "http://content-api", user_id=USER, client=client
            )
            source.fetch_documents(project_id, (REQUEST,))
        return {"trace_id": AppLogger.current().trace_id}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/broken")
    def broken():
        raise RuntimeError("secret bug")

    return app


def test_incoming_traceparent_reaches_lines_and_the_outgoing_content_call(log_lines):
    outgoing = []
    with TestClient(propagation_app(outgoing)) as client:
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
    # 나가는 content 호출이 같은 trace의 traceparent를 싣는다
    (request,) = outgoing
    assert request.headers["traceparent"].startswith(f"00-{TRACE}-")
    assert "secret" not in log_lines.text()


def test_health_404_and_unhandled_errors(log_lines):
    with TestClient(propagation_app([]), raise_server_exceptions=False) as client:
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


# ------------------------------------------------------------------ Kafka 헤더 → 완료 헤더


def test_record_trace_is_stored_with_the_completion_and_sent_by_the_relay(log_lines):
    """Kafka 헤더 T → consumer span → InboxEvent traceparent → relay가 보낸 완료 헤더 T."""
    request = RefreshRequest(
        JobKey(PROJECT, REQUEST, "a" * 64), ArtifactRef("input", "refresh.json"), ()
    )
    completion = Completion(
        request.job, ArtifactRef("result", "refresh.json"), RefreshOutcome.NO_CHANGE
    )
    queries = []

    def neptune(sent):
        query = json.loads(sent.content)["gremlin"]
        queries.append(query)
        data = ["APPLIED"]
        if "outbox_pending','true')" in query and ".project(" in query:
            # 발행 대기 조회: 기록한 traceparent를 그대로 돌려준다
            stored = query_traceparent(queries[0])
            data = [
                {
                    "identifier": f"inbox:graph-rag-refresh:{PROJECT}:{REQUEST}",
                    "receipt": receipt_json(completion),
                    "created_at": "2026-11-04T00:00:00+00:00",
                    "traceparent": stored,
                }
            ]
        if ".property(single,'outbox_pending','false')" in query:
            data = ["SENT"]
        return httpx.Response(
            200, json={"status": {"code": 200}, "result": {"data": data}}
        )

    with httpx.Client(transport=httpx.MockTransport(neptune)) as client:
        inbox = NeptuneRefreshInbox("http://127.0.0.1:8182", client=client)
        headers = (("traceparent", INCOMING.encode()),)
        with LogFlow.kafka_record("t", 0, 0, headers, (), "graph refresh requested"):
            inbox.complete(request, completion)
        stored = query_traceparent(queries[0])
        assert stored.startswith(f"00-{TRACE}-")
        producer = ProducerStub()
        relay = RefreshOutboxRelay(
            inbox, KafkaPublisher(producer, timeout=1, clock=lambda: producer.now)
        )
        assert relay.drain(threading.Event()) == 1
    ((topic, sent),) = producer.outgoing
    assert topic == REFRESH_COMPLETED
    assert dict(sent["headers"])["traceparent"] == stored.encode()
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
    assert published["loresentry.kafka.topic"] == REFRESH_COMPLETED
    assert published["loresentry.kafka.offset"] == 41


def test_completion_without_a_stored_trace_has_no_traceparent_header():
    producer = ProducerStub()
    completion = Completion(
        JobKey(PROJECT, REQUEST, "a" * 64),
        ArtifactRef("result", "refresh.json"),
        RefreshOutcome.NO_CHANGE,
    )
    KafkaPublisher(producer, timeout=1, clock=lambda: producer.now).publish(completion)
    ((_, sent),) = producer.outgoing
    assert "traceparent" not in dict(sent["headers"])


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


def test_spans_are_sent_only_when_the_collector_address_is_set(monkeypatch):
    # 한 번만 설정하는 표시를 풀어 setup_tracing이 provider를 새로 만들게 하고, 만든 provider를 받아 본다
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from app.logs import _trace

    made = []
    monkeypatch.setattr(otel_trace, "set_tracer_provider", made.append)
    monkeypatch.setenv("APP_VERSION", "build-1-1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(_trace, "_configured", False)
    _trace.setup_tracing("graph-rag-test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(_trace, "_configured", False)
    _trace.setup_tracing("graph-rag-test")

    local, dev = made
    assert local._active_span_processor._span_processors == ()
    assert [type(p) for p in dev._active_span_processor._span_processors] == [
        BatchSpanProcessor
    ]
    assert dev.resource.attributes["service.version"] == "build-1-1"
    for provider in made:
        provider.shutdown()
