"""실제 Kafka에서 수신·fake 처리·완료 offset의 재시작 복구를 확인한다."""

import os
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import pytest
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from fastapi.testclient import TestClient

from app.kafka.fake import FakeConsumerHandler, FakeProducer
from app.kafka.polling import PollingWorker, consumer_config
from app.main import create_app

pytestmark = pytest.mark.kafka


def test_real_polling_fake_processing_and_committed_offset():
    bootstrap = os.environ.get("KAFKA_TEST_BOOTSTRAP_SERVERS")
    if not bootstrap:
        pytest.skip("Set KAFKA_TEST_BOOTSTRAP_SERVERS for disposable Kafka integration")
    token = uuid4().hex
    topic = f"graph-rag.sandbox.{token}"
    group = f"graph-rag-fake-{token}"
    admin = AdminClient({"bootstrap.servers": bootstrap})
    admin.create_topics([NewTopic(topic, 1, 1)])[topic].result(timeout=20)
    stop = Event()
    errors = []
    thread = None
    try:
        deliveries = []
        producer = Producer({"bootstrap.servers": bootstrap})
        for value in (b"one", b"two", None):
            producer.produce(
                topic,
                key=b"project",
                value=value,
                headers=[("ce_id", b"fake-event")],
                on_delivery=lambda error, message: deliveries.append(error),
            )
        assert producer.flush(15) == 0
        assert deliveries == [None, None, None]
        fake_producer = FakeProducer()
        fake_handler = FakeConsumerHandler(fake_producer)
        records = []

        def handle(record):
            records.append(record)
            fake_handler(record)
            if fake_producer.count == 3:
                stop.set()

        def run():
            try:
                worker = PollingWorker(
                    Consumer(consumer_config(bootstrap, group)), (topic,), handle
                )
                worker.run(stop)
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=run)
        thread.start()
        thread.join(timeout=25)
        assert not thread.is_alive(), "worker did not stop after three records"
        assert not errors
        assert [record.offset for record in records] == [0, 1, 2]
        assert records[0].headers == (("ce_id", b"fake-event"),)
        assert [receipt.value_bytes for receipt in fake_producer.published] == [3, 3, 0]
        # A fresh client sees the persisted group offset, not process-local state.
        reader = Consumer(consumer_config(bootstrap, group))
        try:
            committed = reader.committed([TopicPartition(topic, 0)], timeout=10)
            assert committed[0].offset == 3
        finally:
            reader.close()
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
        admin.delete_topics([topic])[topic].result(timeout=15)


def test_api_lifespan_polls_and_closes_real_consumer(monkeypatch):
    bootstrap = os.environ.get("KAFKA_TEST_BOOTSTRAP_SERVERS")
    if not bootstrap:
        pytest.skip("Set KAFKA_TEST_BOOTSTRAP_SERVERS for disposable Kafka integration")
    token = uuid4().hex
    topic = f"graph-rag.sandbox.{token}"
    group = f"graph-rag-fake-{token}"
    admin = AdminClient({"bootstrap.servers": bootstrap})
    admin.create_topics([NewTopic(topic, 1, 1)])[topic].result(timeout=20)
    monkeypatch.setenv("KAFKA_POLLING_ENABLED", "true")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", bootstrap)
    monkeypatch.setenv("KAFKA_POLLING_TOPICS", topic)
    monkeypatch.setenv("KAFKA_POLLING_GROUP_ID", group)
    try:
        producer = Producer({"bootstrap.servers": bootstrap})
        producer.produce(topic, key=b"project", value=b"fake input")
        assert producer.flush(10) == 0
        app = create_app()
        with TestClient(app) as client:
            runtime = app.state.kafka_polling
            deadline = monotonic() + 20
            while runtime.producer.count == 0 and monotonic() < deadline:
                assert client.get("/health").status_code == 200
                Event().wait(0.02)
            assert runtime.producer.count == 1
            assert client.get("/health").status_code == 200
        assert not runtime._thread.is_alive()
        assert not runtime.healthy
        reader = Consumer(consumer_config(bootstrap, group))
        try:
            assert (
                reader.committed([TopicPartition(topic, 0)], timeout=10)[0].offset == 1
            )
        finally:
            reader.close()
    finally:
        admin.delete_topics([topic])[topic].result(timeout=15)
