"""전용 Gremlin 프로젝트와 로컬 Content HTTP 서버를 준비·정리한다."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

from app.adapters.neptune_graph import NeptuneProjectionStore
from app.adapters.projection_gremlin import literal


@pytest.fixture
def content_server():
    state = {"responses": {}, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            # 요청은 query까지 기록하고, 응답은 query를 뺀 경로로 찾는다.
            state["requests"].append((self.path, dict(self.headers)))
            status, payload = state["responses"].get(
                self.path.split("?", 1)[0], (404, {"code": "NOT_FOUND"})
            )
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def graph():
    endpoint = os.environ.get("GREMLIN_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("Set GREMLIN_TEST_ENDPOINT for disposable Gremlin integration")
    projects = []
    client = httpx.Client(timeout=20)

    def query(traversal, *, language=None):
        body = {"gremlin": traversal}
        if language:
            body["language"] = language
        response = client.post(
            f"{endpoint}/gremlin",
            json=body,
            headers={"Accept": "application/vnd.gremlin-v3.0+json;types=false"},
        )
        response.raise_for_status()
        payload = response.json()
        assert payload["status"]["code"] == 200, payload
        return payload["result"]["data"]

    def project():
        value = str(uuid4())
        projects.append(value)
        return value

    with NeptuneProjectionStore(endpoint, timeout_seconds=20) as store:
        try:
            yield store, query, project
        finally:
            for project_id in projects:
                query(f"g.V().has('project_id',{literal(project_id)}).drop()")
            client.close()
