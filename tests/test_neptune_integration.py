"""Real HTTP adapter against a loopback server; no AWS credentials required."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.neptune import NeptuneStatusSource

pytestmark = pytest.mark.integration


@pytest.fixture
def upstream():
    state = {"status": 200, "body": b'{"role":"writer"}', "requests": [], "wait": False}
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.command, self.path))
            if state["wait"]:
                release.wait(timeout=5)
            body = state["body"]
            try:
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The timeout test intentionally closes the client first.

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        state["endpoint"] = f"http://127.0.0.1:{server.server_port}"
        try:
            yield state
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=5)


def test_real_adapter_and_route_can_repeat_a_read(upstream):
    source = NeptuneStatusSource(upstream["endpoint"])
    with TestClient(create_app(source)) as client:
        first = client.get("/health/db")
        second = client.get("/health/db")
    assert first.status_code == second.status_code == 200
    assert (
        first.json()
        == second.json()
        == {
            "status": "ok",
            "service": "graph-rag-api",
            "endpoint": upstream["endpoint"],
            "role": "writer",
            "dbEngineVersion": "unknown",
            "gremlin": "unknown",
        }
    )
    assert upstream["requests"] == [("GET", "/status"), ("GET", "/status")]


@pytest.mark.parametrize(
    "status,body",
    [
        (500, b"{}"),
        (200, b"not-json"),
        (200, b"[]"),
        (200, b'{"role":null}'),
    ],
)
def test_upstream_failures_are_503_without_retry(upstream, status, body):
    upstream.update(status=status, body=body)
    with TestClient(create_app(NeptuneStatusSource(upstream["endpoint"]))) as client:
        response = client.get("/health/db")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert upstream["requests"] == [("GET", "/status")]


def test_real_read_timeout_is_503_without_retry(upstream):
    upstream["wait"] = True
    source = NeptuneStatusSource(upstream["endpoint"], timeout_seconds=0.1)
    with TestClient(create_app(source)) as client:
        response = client.get("/health/db")
    assert response.status_code == 503
    assert upstream["requests"] == [("GET", "/status")]
