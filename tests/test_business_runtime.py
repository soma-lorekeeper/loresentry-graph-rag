"""업무별 runtime 수명·설정·시작 실패·종료와 FastAPI health를 확인한다."""

from contextlib import nullcontext
from threading import Event, get_ident

import pytest
from fastapi.testclient import TestClient

from app.kafka.business_runtime import BusinessPollingRuntime, BusinessSettings
from app.kafka.messages import PROJECT_CHANGED, REFRESH_REQUESTED
from app.main import create_app
from tests.fakes.refresh import FakeInbox


def test_settings_disabled_separate_from_fake_and_require_explicit_enable():
    assert BusinessSettings.from_env({}) is None
    settings = BusinessSettings.from_env(
        {
            "KAFKA_PROJECT_CHANGED_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": "broker:9092",
        }
    )
    assert settings.topics == (PROJECT_CHANGED,)
    for env in (
        {"KAFKA_REFRESH_REQUESTED_ENABLED": "yes"},
        {"KAFKA_REFRESH_REQUESTED_ENABLED": "true"},
        {
            "KAFKA_PROJECT_CHANGED_ENABLED": "true",
            "KAFKA_POLLING_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": "broker",
        },
    ):
        with pytest.raises(ValueError):
            BusinessSettings.from_env(env)


def test_two_subscriptions_have_independent_owned_threads_and_cleanup(monkeypatch):
    threads = []
    closed = []
    running = [Event(), Event()]

    class Producer:
        def __init__(self, config):
            threads.append(get_ident())

        def flush(self, timeout):
            closed.append(get_ident())

    class Consumer:
        def __init__(self, config):
            self.group = config["group.id"]
            threads.append(get_ident())

    def handlers(topic, publisher, stop, stack, settings):
        return (
            {"FileChanged": lambda _: None, "ProjectDeleted": lambda _: None}
            if topic == PROJECT_CHANGED
            else {"GraphRefreshRequested": lambda _: None}
        )

    def worker(consumer, topic, handlers, publisher, stop):
        class Worker:
            def run(self, event):
                assert consumer.group.startswith("graph-rag.")
                running[0 if topic == PROJECT_CHANGED else 1].set()
                assert event.wait(3)

        return Worker()

    monkeypatch.setattr("app.kafka.business_runtime.business_worker", worker)
    runtime = BusinessPollingRuntime(
        BusinessSettings("broker", (PROJECT_CHANGED, REFRESH_REQUESTED)),
        handler_factory=handlers,
        consumer_factory=Consumer,
        producer_factory=Producer,
        outbox_factory=lambda: nullcontext(FakeInbox()),
    )
    runtime.start()
    assert all(flag.wait(1) for flag in running)
    assert runtime.healthy
    runtime.close()
    assert not runtime.healthy
    assert len(set(threads)) == 3
    assert get_ident() not in threads
    assert len(closed) == 3
    with pytest.raises(RuntimeError):
        runtime.start()


def test_startup_failure_closes_resources_and_app_health_tracks_runtime(monkeypatch):
    class Producer:
        def __init__(self, config):
            pass

        def flush(self, timeout):
            pass

    def fail(*args):
        raise ValueError("dependency missing")

    runtime = BusinessPollingRuntime(
        BusinessSettings("broker", (PROJECT_CHANGED,)),
        handler_factory=fail,
        producer_factory=Producer,
    )
    with pytest.raises(RuntimeError, match="failed to start"):
        runtime.start()
    assert not runtime.healthy
    assert all(not thread.is_alive() for thread in runtime._threads)

    class Runtime:
        healthy = True

        def start(self):
            pass

        def close(self):
            self.closed = True

    live = Runtime()
    monkeypatch.setenv("KAFKA_POLLING_ENABLED", "false")
    monkeypatch.setattr("app.main.BusinessPollingRuntime.from_env", lambda _: live)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        live.healthy = False
        assert client.get("/health").status_code == 503
    assert live.closed
