"""실제 boto3와 로컬 S3 HTTP 대체 서버. 실제 AWS·자격증명·원격 LLM은 사용하지 않는다."""

import base64
import json
import threading
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import boto3
import pytest
from botocore.config import Config

from app.adapters.refresh_artifacts import S3ArtifactSettings, S3RefreshArtifacts
from app.adapters.refresh_schema import (
    ContextObject,
    fingerprint,
    input_bytes,
    json_bytes,
)
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import Outcome
from evaluation.cases import new_setting_cases
from evaluation.runner import CallBudget, assemble

pytestmark = pytest.mark.integration


@pytest.fixture
def s3_server():
    state = {"objects": {}, "requests": [], "failures": {}, "lost_ack": set()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, status, body, headers=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, code):
            self.respond(
                status,
                f"<Error><Code>{code}</Code><Message>private upstream body</Message></Error>".encode(),
                {"Content-Type": "application/xml"},
            )

        def handle_object(self, method):
            path = unquote(urlsplit(self.path).path).lstrip("/")
            body = (
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if method == "PUT"
                else b""
            )
            state["requests"].append((method, path, dict(self.headers), body))
            if (method, path) in state["failures"]:
                self.error(*state["failures"][(method, path)])
                return
            if method == "PUT":
                assert self.headers.get("If-None-Match") == "*"
                assert (
                    self.headers.get("x-amz-checksum-sha256")
                    == base64.b64encode(sha256(body).digest()).decode()
                )
                if path in state["objects"]:
                    self.error(412, "PreconditionFailed")
                    return
                metadata = {
                    k[11:]: v
                    for k, v in self.headers.items()
                    if k.lower().startswith("x-amz-meta-")
                }
                state["objects"][path] = (body, metadata)
                if path in state["lost_ack"]:
                    state["lost_ack"].remove(path)
                    self.error(500, "InternalError")
                    return
                self.respond(200, b"", {"ETag": '"local-etag"'})
                return
            if path not in state["objects"]:
                self.error(404, "NoSuchKey")
                return
            value, metadata = state["objects"][path]
            self.respond(
                200,
                value,
                {
                    "Content-Type": "application/json",
                    **{f"x-amz-meta-{k}": v for k, v in metadata.items()},
                },
            )

        def do_GET(self):
            self.handle_object("GET")

        def do_PUT(self):
            self.handle_object("PUT")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url=f"http://127.0.0.1:{server.server_port}",
        aws_access_key_id="local-test-only",
        aws_secret_access_key="local-test-only",
        config=Config(
            s3={"addressing_style": "path"},
            retries={"total_max_attempts": 1},
            connect_timeout=1,
            read_timeout=2,
        ),
    )
    try:
        yield (
            state,
            S3RefreshArtifacts(
                client, S3ArtifactSettings("artifact-bucket", "input-bucket")
            ),
        )
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()


def setup_case(state, store):
    case = new_setting_cases("gpt-5.6-luna", "s3-test")[1]
    body = input_bytes(case.source)
    request = replace(
        case.request,
        job=replace(case.request.job, input_fingerprint=fingerprint(body)),
        input_ref=replace(
            case.request.input_ref, bucket="input-bucket", key="refresh/input.json"
        ),
    )
    case = replace(case, request=request, source=replace(case.source, job=request.job))
    state["objects"]["input-bucket/refresh/input.json"] = (body, {})
    scenario = assemble(case, None, CallBudget(2))
    scenario.service.artifacts = store
    return case, scenario


def test_s3_service_roundtrip_and_restart_preserve_input_and_skip_model(s3_server):
    state, store = s3_server
    case, scenario = setup_case(state, store)
    original = store.read_input(case.request)
    completion = scenario.service.run(case.request, "s3-1")
    assert completion.outcome == Outcome.PROPOSED
    assert completion.result_ref.bucket == "artifact-bucket"
    result = store.read_result(case.request)
    assert result.new_document_proposals == case.expected.new_document_proposals
    assert store.read_input(case.request) == original
    assert (
        next(
            d.body_text
            for d in store.read_context(case.request).documents
            if d.document_id == original.documents[0].document_id
        )
        == original.documents[0].body_text
    )
    # A new adapter/service has no in-memory artifact history. S3 alone recovers
    # the result even if the original input and execution context have expired.
    state["objects"].pop("input-bucket/refresh/input.json")
    state["objects"].pop("artifact-bucket/" + store._ref(case.request, "context").key)
    restarted = assemble(case, None, CallBudget(1))
    restarted.service.artifacts = S3RefreshArtifacts(store.client, store.settings)
    done = restarted.service.run(case.request, "s3-2")
    assert done == completion
    assert not any(name == "model.generate" for name in restarted.calls.names())
    assert restarted.service.model.budget.used == 0


def test_snapshot_is_immutable_and_original_input_cannot_change(s3_server):
    state, store = s3_server
    case, scenario = setup_case(state, store)
    scenario.service.run(case.request, "s3-1")
    snapshot = store.read_context(case.request)
    assert store.save_context(snapshot) == store._ref(case.request, "context")
    altered = replace(
        snapshot,
        documents=tuple(
            replace(d, body_text=d.body_text + "\n")
            if d.folder_code == "MANUSCRIPT"
            else d
            for d in snapshot.documents
        ),
    )
    with pytest.raises(RequestConflict):
        store.save_context(altered)
    state["objects"].pop("artifact-bucket/" + store._ref(case.request, "context").key)
    with pytest.raises(RefreshFailure) as caught:
        store.save_context(altered)
    assert caught.value.failure.code == "S3_OBJECT_INVALID"


def test_input_hash_scope_and_missing_objects(s3_server):
    state, store = s3_server
    case, _ = setup_case(state, store)
    assert store.read_context(case.request) is None
    assert store.read_result(case.request) is None
    changed = replace(
        case.request, job=replace(case.request.job, input_fingerprint="wrong")
    )
    with pytest.raises(RefreshFailure) as caught:
        store.read_input(changed)
    assert caught.value.failure.code == "INPUT_FINGERPRINT_MISMATCH"
    for ref in (
        replace(case.request.input_ref, bucket="other-bucket"),
        replace(case.request.input_ref, key="unrelated/input.json"),
    ):
        with pytest.raises(RefreshFailure) as caught:
            store.read_input(replace(case.request, input_ref=ref))
        assert caught.value.failure.code == "S3_INPUT_LOCATION_INVALID"
    state["objects"].clear()
    with pytest.raises(RefreshFailure) as caught:
        store.read_input(case.request)
    assert caught.value.failure.code == "S3_OBJECT_MISSING"


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (403, "AccessDenied", False),
        (404, "NoSuchBucket", False),
        (503, "SlowDown", True),
    ],
)
def test_s3_errors_are_sanitized_and_not_retried(s3_server, status, code, retryable):
    state, store = s3_server
    case, _ = setup_case(state, store)
    state["failures"][("GET", "input-bucket/refresh/input.json")] = (status, code)
    with pytest.raises(RefreshFailure) as caught:
        store.read_input(case.request)
    assert caught.value.failure.retryable == retryable
    assert "private upstream" not in str(caught.value)
    assert (
        len(
            [
                r
                for r in state["requests"]
                if r[:2] == ("GET", "input-bucket/refresh/input.json")
            ]
        )
        == 1
    )


def test_write_race_is_idempotent_or_conflict_and_409_is_retryable(s3_server):
    state, store = s3_server
    case, _ = setup_case(state, store)
    ref = store._ref(case.request, "request")
    body = b'{"example":1}'
    assert store._put(ref, body) == store._put(ref, body)
    with pytest.raises(RequestConflict):
        store._put(ref, b'{"example":2}')
    state["failures"][("PUT", "artifact-bucket/" + ref.key)] = (
        409,
        "ConditionalRequestConflict",
    )
    with pytest.raises(RefreshFailure) as caught:
        store._put(ref, body)
    assert caught.value.failure.retryable


def test_saved_result_after_lost_ack_is_recovered_without_model(s3_server):
    state, store = s3_server
    case, scenario = setup_case(state, store)
    state["lost_ack"].add("artifact-bucket/" + store._ref(case.request, "result").key)
    with pytest.raises(RefreshFailure) as caught:
        scenario.service.run(case.request, "s3-1")
    assert caught.value.failure.retryable
    budget = scenario.service.model.budget
    assert budget.used == 1
    assert scenario.service.run(case.request, "s3-2").outcome == Outcome.PROPOSED
    assert budget.used == 1


def test_same_id_with_other_settings_and_corrupt_object_are_rejected(s3_server):
    state, store = s3_server
    case, scenario = setup_case(state, store)
    scenario.service.run(case.request, "s3-1")
    with pytest.raises(RequestConflict):
        store.read_result(replace(case.request, discover_related=False))
    path = "artifact-bucket/" + store._ref(case.request, "result").key
    body, metadata = state["objects"][path]
    state["objects"][path] = (body + b" ", metadata)
    with pytest.raises(RefreshFailure) as caught:
        store.read_result(case.request)
    assert caught.value.failure.code == "S3_OBJECT_INVALID"


def test_context_json_keeps_whitespace_emoji_and_null_inactive_body(s3_server):
    from app.refresh.models import DocumentState

    state, store = s3_server
    case, _ = setup_case(state, store)
    text = "  민수\r\n\n🐈 같은 말. 같은 말.  "
    docs = (
        replace(case.source.documents[0], body_text=text),
        replace(
            case.source.documents[0],
            document_id="deleted",
            state=DocumentState.DELETED,
            body_text=None,
        ),
    )
    source = replace(case.source, documents=docs)
    body = input_bytes(source)
    request = replace(
        case.request,
        job=replace(case.request.job, input_fingerprint=fingerprint(body)),
        changed_documents=(),
    )
    state["objects"]["input-bucket/refresh/input.json"] = (body, {})
    read = store.read_input(request)
    assert read.documents == docs
    from app.refresh.models import ExecutionSnapshot

    snapshot = ExecutionSnapshot(
        request, docs, case.related, changed_ids=tuple(d.document_id for d in docs)
    )
    store.save_context(snapshot)
    assert store.read_context(request) == snapshot
    encoded = ContextObject(
        schema_version="refresh-context-v1", snapshot=snapshot
    ).model_dump(mode="json")
    assert (
        json.loads(json_bytes(encoded))["snapshot"]["documents"][0]["body_text"] == text
    )
