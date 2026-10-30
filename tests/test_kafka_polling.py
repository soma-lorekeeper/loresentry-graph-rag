"""브로커 없이 polling 수명·처리/commit 경계·재할당을 재현한다."""

from collections import deque
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from confluent_kafka import KafkaError, KafkaException, TopicPartition

from app.kafka.fake import FakeConsumerHandler, FakeProducer
from app.kafka.polling import PollingWorker, consumer_config


class Message:
    def __init__(self, offset=0, error=None):
        self._offset = offset
        self._error = error

    def topic(self):
        return "graph-rag.sandbox.test"

    def partition(self):
        return 0

    def offset(self):
        return self._offset

    def key(self):
        return b"project"

    def value(self):
        return None if self._offset else b"hello"

    def headers(self):
        return [("ce_id", b"event"), ("optional", None)]

    def error(self):
        return self._error


class Transport:
    def __init__(self, stop, messages=()):
        self.stop = stop
        self.messages = deque(messages)
        self.paused = False
        self.closed = False
        self.commits = []
        self.threads = set()
        self.polls = 0
        self.on_poll = None
        self.commit_error = None
        self.stop_on_commit = True

    def subscribe(self, topics, **callbacks):
        self.callbacks = callbacks
        self.partitions = [TopicPartition(topics[0], 0)]
        self.callbacks["on_assign"](self, self.partitions)

    def assign(self, partitions):
        self.threads.add(get_ident())
        self.paused = False

    def unassign(self):
        self.threads.add(get_ident())

    def pause(self, partitions):
        self.threads.add(get_ident())
        self.paused = True

    def resume(self, partitions):
        self.threads.add(get_ident())
        self.paused = False

    def poll(self, timeout):
        self.threads.add(get_ident())
        self.polls += 1
        assert self.polls < 1000, "polling did not terminate"
        if self.on_poll:
            self.on_poll(self)
        if self.messages and not self.paused:
            return self.messages.popleft()
        Event().wait(0.001)

    def commit(self, *, offsets, asynchronous):
        self.threads.add(get_ident())
        assert asynchronous is False
        if isinstance(self.commit_error, Exception):
            raise self.commit_error
        self.commits.extend((p.topic, p.partition, p.offset) for p in offsets)
        if self.stop_on_commit:
            self.stop.set()
        return [SimpleNamespace(error=self.commit_error)]

    def close(self):
        self.threads.add(get_ident())
        self.closed = True


def worker(transport, handler):
    return PollingWorker(transport, ("graph-rag.sandbox.test",), handler)


def test_manual_offset_config():
    config = consumer_config("localhost:9092", "graph-rag-fake-test")
    assert config["enable.auto.commit"] is False
    assert config["enable.auto.offset.store"] is False
    assert config["allow.auto.create.topics"] is False
    assert config["max.partition.fetch.bytes"] == 6291456
    assert config["group.protocol"] == "classic"
    with pytest.raises(ValueError):
        consumer_config("", "group")


def test_fake_handling_and_exact_commit_in_order():
    stop = Event()
    transport = Transport(stop, [Message(), Message(1)])
    transport.stop_on_commit = False
    producer = FakeProducer()
    handler = FakeConsumerHandler(producer)
    records = []
    handler_threads = set()

    def handle(record):
        handler_threads.add(get_ident())
        records.append(record)
        handler(record)
        if producer.count == 2:
            stop.set()

    runtime = worker(transport, handle)
    runtime.run(stop)
    assert [item.offset for item in producer.published] == [0, 1]
    assert [item.value_bytes for item in producer.published] == [5, 0]
    assert records[0].headers == (("ce_id", b"event"), ("optional", None))
    assert transport.commits == [(records[0].topic, 0, 1), (records[0].topic, 0, 2)]
    assert transport.threads == {get_ident()}
    assert not transport.threads & handler_threads
    assert transport.closed
    with pytest.raises(RuntimeError, match="only run once"):
        runtime.run(stop)


def test_poll_continues_without_commit_while_handler_blocks():
    stop = Event()
    release = Event()
    transport = Transport(stop, [Message()])

    def on_poll(client):
        if client.polls == 8:
            assert not client.commits
            assert client.paused
            release.set()

    transport.on_poll = on_poll

    def handle(record):
        assert release.wait(2)

    worker(transport, handle).run(stop)
    assert transport.polls >= 8
    assert transport.commits[0][2] == 1


def test_handler_failure_never_commits_or_processes_later_record():
    stop = Event()
    transport = Transport(stop, [Message(), Message(1)])
    seen = []

    def fail(record):
        seen.append(record.offset)
        raise ValueError("fake producer failed")

    with pytest.raises(ValueError, match="fake producer failed"):
        worker(transport, fail).run(stop)
    assert seen == [0]
    assert not transport.commits
    assert transport.closed


@pytest.mark.parametrize("callback", ["on_revoke", "on_lost"])
def test_rebalance_discards_old_completion_even_if_partition_reassigned(callback):
    stop = Event()
    release = Event()
    transport = Transport(stop, [Message()])

    def on_poll(client):
        if client.polls == 5:
            client.callbacks[callback](client, client.partitions)
            client.callbacks["on_assign"](client, client.partitions)
            assert client.paused
            release.set()
            stop.set()

    transport.on_poll = on_poll

    def handle(record):
        assert release.wait(2)

    worker(transport, handle).run(stop)
    assert not transport.commits
    assert transport.closed


@pytest.mark.parametrize(
    "error", [KafkaError(KafkaError._FAIL), RuntimeError("commit")]
)
def test_commit_error_stops_before_next_record(error):
    stop = Event()
    transport = Transport(stop, [Message(), Message(1)])
    transport.commit_error = error
    seen = []
    with pytest.raises((KafkaException, RuntimeError)):
        worker(transport, lambda record: seen.append(record.offset)).run(stop)
    assert seen == [0]
    assert transport.closed


def test_poll_error_closes_without_commit():
    stop = Event()
    transport = Transport(stop, [Message(error=KafkaError(KafkaError._FAIL))])
    with pytest.raises(KafkaException):
        worker(transport, lambda _: pytest.fail("must not handle error")).run(stop)
    assert not transport.commits
    assert transport.closed


def test_idle_shutdown_and_eof_do_not_call_handler():
    stop = Event()
    transport = Transport(stop, [Message(error=KafkaError(KafkaError._PARTITION_EOF))])
    transport.on_poll = lambda client: stop.set() if client.polls == 3 else None
    worker(transport, lambda _: pytest.fail("unexpected record")).run(stop)
    assert not transport.commits
    assert transport.closed


def test_stop_during_poll_leaves_new_message_uncommitted():
    stop = Event()
    transport = Transport(stop, [Message()])
    transport.on_poll = lambda client: stop.set()
    worker(transport, lambda _: pytest.fail("stopping")).run(stop)
    assert not transport.commits
    assert transport.closed


@pytest.mark.parametrize(
    ("topic", "group"),
    [
        ("content.project.changed.v2", "graph-rag-fake-test"),
        ("graph-rag.sandbox.test", "graph-rag-production"),
    ],
)
def test_fake_cli_rejects_business_topics_and_groups(monkeypatch, topic, group):
    from scripts.poll_kafka import main

    monkeypatch.setattr(
        "sys.argv",
        [
            "poll_kafka",
            "--bootstrap-servers",
            "localhost:9092",
            "--topic",
            topic,
            "--group-id",
            group,
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
