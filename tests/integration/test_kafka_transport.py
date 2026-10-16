"""일회용 broker에서 Content 포장 수신→완료/DLQ 발행→offset 영속화를 검증한다."""

import json
import os
from dataclasses import replace
from threading import Event, Thread
from uuid import uuid4

import pytest
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from app.kafka.consumer import business_worker
from app.kafka.messages import REFRESH_COMPLETED, REFRESH_REQUESTED
from app.kafka.polling import consumer_config
from app.kafka.publisher import KafkaPublisher, producer_config
from app.refresh.models import ArtifactRef, Completion, JobKey, Outcome
from tests.test_kafka_messages import PROJECT, REQUEST, content_record

pytestmark = pytest.mark.kafka


@pytest.fixture
def broker_topics():
    bootstrap = os.environ.get("KAFKA_TEST_BOOTSTRAP_SERVERS")
    if not bootstrap:
        pytest.skip("Set KAFKA_TEST_BOOTSTRAP_SERVERS to a disposable broker")
    topics = [
        REFRESH_REQUESTED,
        REFRESH_COMPLETED,
        REFRESH_REQUESTED + ".graph-rag.dlq",
    ]
    admin = AdminClient({"bootstrap.servers": bootstrap})
    existing = admin.list_topics(timeout=10).topics
    if any(topic in existing for topic in topics):
        pytest.fail("Transport test needs a disposable broker without business topics")
    created = []
    try:
        for topic in topics:
            admin.create_topics([NewTopic(topic, 3, 1)])[topic].result(timeout=15)
            created.append(topic)
        yield bootstrap
    finally:
        for future in admin.delete_topics(created).values():
            future.result(timeout=15)


@pytest.mark.parametrize("malformed", [False, True])
def test_business_transport_round_trip_and_commit(broker_topics, malformed):
    bootstrap = broker_topics
    group = "graph-rag-transport-test-" + uuid4().hex
    stop = Event()
    errors = []
    deliveries = []
    record = content_record()
    if malformed:
        record = replace(record, value=b"private invalid payload")
    producer = Producer(producer_config(bootstrap))
    producer.produce(
        record.topic,
        partition=2,
        key=record.key,
        value=record.value,
        headers=list(record.headers),
        on_delivery=lambda error, message: deliveries.append(error),
    )
    assert producer.flush(10) == 0
    assert deliveries == [None]

    def run():
        try:
            transport = KafkaPublisher(Producer(producer_config(bootstrap)))

            class DeadLetters:
                def dead_letter(self, original, failure):
                    transport.dead_letter(original, failure)
                    stop.set()

            def handle(event):
                transport.publish(
                    Completion(
                        JobKey(event.project_id, REQUEST, "a" * 64),
                        ArtifactRef("private", "refresh/result.json"),
                        Outcome.NO_CHANGE,
                        prompt_version="refresh-prompt-v13",
                    )
                )
                stop.set()

            worker = business_worker(
                Consumer(consumer_config(bootstrap, group)),
                REFRESH_REQUESTED,
                {"GraphRefreshRequested": handle},
                DeadLetters(),
                stop,
            )
            worker.run(stop)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    try:
        thread.join(timeout=25)
        assert not thread.is_alive()
        assert not errors
        output_topic = (
            REFRESH_REQUESTED + ".graph-rag.dlq" if malformed else REFRESH_COMPLETED
        )
        reader = Consumer(consumer_config(bootstrap, "read-" + uuid4().hex))
        try:
            reader.subscribe([output_topic])
            message = reader.poll(15)
            assert message is not None
            assert message.error() is None
            assert message.key() == PROJECT.encode()
            headers = dict(message.headers())
            if malformed:
                assert message.value() == record.value
                assert (
                    int.from_bytes(
                        headers["kafka_dlt-original-partition"], "big", signed=True
                    )
                    == 2
                )
                assert headers["kafka_dlt-original-topic"] == REFRESH_REQUESTED.encode()
            else:
                assert headers["ce_type"] == b"GraphRefreshCompleted"
                assert headers["ce_subject"] == REQUEST.encode()
                assert json.loads(message.value())["outcome"] == "SUCCEEDED"
        finally:
            reader.close()
        reader = Consumer(consumer_config(bootstrap, group))
        try:
            committed = reader.committed(
                [TopicPartition(REFRESH_REQUESTED, 2)], timeout=10
            )
            assert committed[0].offset == 1
        finally:
            reader.close()
    finally:
        stop.set()
        thread.join(timeout=5)


@pytest.mark.gremlin
def test_durable_completion_relay_republishes_same_event_after_restart(
    broker_topics, graph
):
    from app.adapters.refresh_inbox import NeptuneRefreshInbox
    from app.kafka.errors import DeliveryError
    from app.refresh.outbox import RefreshOutboxRelay
    from tests.test_refresh_inbox import completion, request

    store, _, project = graph
    req = request(project())
    done = completion(req)
    with NeptuneRefreshInbox(store.endpoint) as inbox:
        inbox.complete(req, done)
        pending = next(item for item in inbox.pending() if item.completion == done)
        transport = KafkaPublisher(Producer(producer_config(broker_topics)))

        class LostCommit:
            def pending(self):
                return [pending]

            def mark_sent(self, entry):
                raise DeliveryError("process ended before sent commit")

        with pytest.raises(DeliveryError):
            RefreshOutboxRelay(LostCommit(), transport).drain(Event())
        assert inbox.find(req) == done

    # Fresh clients recover without any incoming request or S3/model access.
    with NeptuneRefreshInbox(store.endpoint) as fresh:
        transport = KafkaPublisher(Producer(producer_config(broker_topics)))
        RefreshOutboxRelay(fresh, transport).drain(Event())
        assert pending not in fresh.pending()
        fresh.complete(req, done)
        assert pending not in fresh.pending()

    reader = Consumer(consumer_config(broker_topics, "relay-read-" + uuid4().hex))
    try:
        reader.assign([TopicPartition(REFRESH_COMPLETED, i, 0) for i in range(3)])
        messages = reader.consume(2, timeout=10)
        assert len(messages) == 2
        assert all(message.error() is None for message in messages)
        assert messages[0].value() == messages[1].value()
        assert messages[0].key() == messages[1].key() == req.job.project_id.encode()
        assert dict(messages[0].headers()) == dict(messages[1].headers())
    finally:
        reader.close()
