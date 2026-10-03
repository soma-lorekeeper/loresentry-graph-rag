import pytest
from fastapi.testclient import TestClient

from app.application import GraphStatusUnavailable
from app.main import create_app


class StubStatusSource:
    endpoint = "https://neptune.test:8182"

    def __init__(self, payload=None, error=None):
        self.payload = {} if payload is None else payload
        self.error = error
        self.calls = 0

    def fetch_status(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def test_basic_routes_do_not_query_graph():
    source = StubStatusSource(error=AssertionError("must not call graph"))
    with TestClient(create_app(source)) as client:
        health = client.get("/health")
        root = client.get("/")
    assert health.status_code == root.status_code == 200
    assert health.json() == {"status": "ok"}
    assert root.json() == {"service": "graph-rag-api"}
    assert source.calls == 0


def test_health_db_reports_reachable_graph():
    source = StubStatusSource({
        "role": "writer", "dbEngineVersion": "1.4.8.0", "gremlin": {"version": "3.7.1"},
    })
    with TestClient(create_app(source)) as client:
        response = client.get("/health/db")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "service": "graph-rag-api", "endpoint": source.endpoint,
        "role": "writer", "dbEngineVersion": "1.4.8.0", "gremlin": "3.7.1",
    }
    assert source.calls == 1


def test_health_db_reports_unreachable_graph_without_retry():
    source = StubStatusSource(error=GraphStatusUnavailable("connection refused"))
    with TestClient(create_app(source)) as client:
        response = client.get("/health/db")
    assert response.status_code == 503
    assert response.json() == {
        "status": "error", "service": "graph-rag-api", "error": "connection refused",
    }
    assert source.calls == 1


def test_health_db_rejects_malformed_snapshot():
    with TestClient(create_app(StubStatusSource({"gremlin": None}))) as client:
        response = client.get("/health/db")
    assert response.status_code == 503
    assert response.json()["error"] == "Graph status gremlin must be a JSON object"


def test_unexpected_bug_is_not_reported_as_graph_unavailability():
    source = StubStatusSource(error=RuntimeError("implementation bug"))
    with TestClient(create_app(source)) as client:
        with pytest.raises(RuntimeError, match="implementation bug"):
            client.get("/health/db")


def test_apps_have_independent_dependencies():
    first = StubStatusSource({"role": "writer"})
    second = StubStatusSource({"role": "reader"})
    with TestClient(create_app(first)) as a, TestClient(create_app(second)) as b:
        assert a.get("/health/db").json()["role"] == "writer"
        assert b.get("/health/db").json()["role"] == "reader"
        assert a.get("/health/db").json()["role"] == "writer"
    assert (first.calls, second.calls) == (2, 1)
