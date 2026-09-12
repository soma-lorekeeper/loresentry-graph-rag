from fastapi.testclient import TestClient

from app import graph
from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_returns_service_name():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"service": "graph-rag-api"}


def test_health_db_reports_reachable_graph(monkeypatch):
    monkeypatch.setattr(
        graph,
        "check",
        lambda: {
            "endpoint": "https://neptune.test:8182",
            "role": "writer",
            "dbEngineVersion": "1.4.8.0",
            "gremlin": "3.7.1",
        },
    )

    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "graph-rag-api",
        "endpoint": "https://neptune.test:8182",
        "role": "writer",
        "dbEngineVersion": "1.4.8.0",
        "gremlin": "3.7.1",
    }


def test_health_db_reports_unreachable_graph(monkeypatch):
    def fail() -> dict[str, str]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(graph, "check", fail)

    response = client.get("/health/db")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "service": "graph-rag-api",
        "error": "connection refused",
    }
