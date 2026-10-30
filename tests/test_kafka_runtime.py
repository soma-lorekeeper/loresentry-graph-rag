"""FastAPI 수명주기·설정·worker 실패의 health 반영을 검증한다."""

from threading import Event, get_ident

import pytest
from fastapi.testclient import TestClient

from app.kafka.runtime import FakePollingSettings
from app.main import create_app
from tests.test_main import StubStatusSource


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("KAFKA_POLLING_ENABLED", "true")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.test:9092")
    monkeypatch.setenv("KAFKA_POLLING_TOPICS", "graph-rag.sandbox.test")
    monkeypatch.setenv("KAFKA_POLLING_GROUP_ID", "graph-rag-fake-test")


@pytest.mark.parametrize(
    "env",
    [
        {"KAFKA_POLLING_ENABLED": "yes"},
        {"KAFKA_POLLING_ENABLED": "true"},
        {
            "KAFKA_POLLING_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": "broker:9092",
            "KAFKA_POLLING_TOPICS": "content.project.changed.v2",
        },
        {
            "KAFKA_POLLING_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": "broker:9092",
            "KAFKA_POLLING_GROUP_ID": "graph-rag-production",
        },
    ],
)
def test_invalid_config_fails_explicitly(env):
    with pytest.raises(ValueError):
        FakePollingSettings.from_env(env)


def test_disabled_by_default_and_normalized_settings():
    assert FakePollingSettings.from_env({}) is None
    settings = FakePollingSettings.from_env(
        {
            "KAFKA_POLLING_ENABLED": " TRUE ",
            "KAFKA_BOOTSTRAP_SERVERS": " broker:9092 ",
            "KAFKA_POLLING_TOPICS": "graph-rag.sandbox.one, graph-rag.sandbox.two",
        }
    )
    assert settings.bootstrap_servers == "broker:9092"
    assert settings.topics == ("graph-rag.sandbox.one", "graph-rag.sandbox.two")


def test_disabled_app_never_constructs_consumer(monkeypatch):
    monkeypatch.setenv("KAFKA_POLLING_ENABLED", "false")
    monkeypatch.setattr(
        "app.kafka.runtime.Consumer", lambda _: pytest.fail("Kafka must be disabled")
    )
    with TestClient(create_app(StubStatusSource())) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_app_starts_and_closes_worker_on_owned_thread(monkeypatch, enabled):
    running = Event()
    closed = Event()
    threads = []

    def consumer(config):
        threads.append(get_ident())
        assert config["enable.auto.commit"] is False
        return object()

    class Worker:
        def __init__(self, consumer, topics, handler):
            threads.append(get_ident())
            assert topics == ("graph-rag.sandbox.test",)

        def run(self, stop):
            threads.append(get_ident())
            running.set()
            assert stop.wait(3)
            closed.set()

    monkeypatch.setattr("app.kafka.runtime.Consumer", consumer)
    monkeypatch.setattr("app.kafka.runtime.PollingWorker", Worker)
    app = create_app(StubStatusSource())
    with TestClient(app) as client:
        assert running.wait(1)
        assert client.get("/health").status_code == 200
        runtime = app.state.kafka_polling
        thread = runtime._thread
    assert closed.is_set()
    assert not thread.is_alive()
    assert not runtime.healthy
    assert len(set(threads)) == 1
    assert threads[0] != get_ident()
    with pytest.raises(RuntimeError, match="only start once"):
        runtime.start()


def test_consumer_creation_failure_fails_startup(monkeypatch, enabled):
    def fail(config):
        raise ValueError("bad config")

    monkeypatch.setattr("app.kafka.runtime.Consumer", fail)
    with pytest.raises(RuntimeError, match="failed to start"):
        with TestClient(create_app(StubStatusSource())):
            pytest.fail("startup must fail")


def test_background_failure_makes_health_unavailable(monkeypatch, enabled):
    crash = Event()
    monkeypatch.setattr("app.kafka.runtime.Consumer", lambda _: object())

    class Worker:
        def __init__(self, *args):
            pass

        def run(self, stop):
            assert crash.wait(3)
            raise RuntimeError("poll failed")

    monkeypatch.setattr("app.kafka.runtime.PollingWorker", Worker)
    app = create_app(StubStatusSource())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        crash.set()
        app.state.kafka_polling._thread.join(timeout=1)
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["error"] == "Kafka polling worker unavailable"
        assert client.get("/").status_code == 200
