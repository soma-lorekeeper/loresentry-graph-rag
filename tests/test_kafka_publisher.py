"""큐 수락과 실제 전달 확인, 큐 포화·늦은 응답 경계를 재현한다."""

import pytest

from app.kafka.errors import DeliveryError
from app.kafka.messages import OutgoingRecord
from app.kafka.publisher import KafkaPublisher, producer_config


class ProducerStub:
    def __init__(self):
        self.now = 0.0
        self.callback = None
        self.outgoing = []
        self.full = 0
        self.ack_after = 0.3
        self.delivery_error = None
        self.enqueue_error = None
        self.poll_error = None

    def produce(self, topic, **kwargs):
        if self.enqueue_error:
            raise self.enqueue_error
        if self.full:
            self.full -= 1
            raise BufferError("queue full")
        self.outgoing.append((topic, kwargs))
        self.callback = kwargs["on_delivery"]

    def poll(self, timeout):
        if self.poll_error:
            raise self.poll_error
        self.now += timeout
        if self.callback and self.now >= self.ack_after:
            callback, self.callback = self.callback, None
            callback(self.delivery_error, object())


def publisher(producer, timeout=1):
    return KafkaPublisher(producer, timeout=timeout, clock=lambda: producer.now)


def record():
    return OutgoingRecord("test", b"key", b"value", (("ce_id", b"id"),))


def test_only_callback_success_confirms_delivery_and_no_partition_is_forced():
    producer = ProducerStub()
    producer.full = 2
    publisher(producer).send(record())
    assert producer.now >= producer.ack_after
    assert len(producer.outgoing) == 1
    topic, outgoing = producer.outgoing[0]
    assert topic == "test"
    assert outgoing["headers"] == [("ce_id", b"id")]
    assert "partition" not in outgoing


@pytest.mark.parametrize("stage", ["enqueue", "poll", "callback", "timeout", "queue"])
def test_failure_never_returns_success_or_exposes_external_message(stage):
    producer = ProducerStub()
    if stage == "enqueue":
        producer.enqueue_error = RuntimeError("secret")
    elif stage == "poll":
        producer.poll_error = RuntimeError("secret")
    elif stage == "callback":
        producer.delivery_error = RuntimeError("secret")
    elif stage == "timeout":
        producer.ack_after = 10
    else:
        producer.full = 100
    with pytest.raises(DeliveryError) as error:
        publisher(producer).send(record())
    assert "secret" not in str(error.value)
    assert producer.now <= 1.01


def test_late_callback_does_not_confirm_next_message():
    producer = ProducerStub()
    producer.ack_after = 10
    transport = publisher(producer)
    with pytest.raises(DeliveryError):
        transport.send(record())
    producer.callback(None, object())
    with pytest.raises(DeliveryError):
        transport.send(record())
    assert len(producer.outgoing) == 2


def test_producer_configuration_uses_librdkafka_names():
    settings = producer_config("localhost:19092")
    assert settings["enable.idempotence"] is True
    assert settings["acks"] == "all"
    assert settings["message.timeout.ms"] == 20000
    assert settings["message.max.bytes"] == 6291456
    assert "max.request.size" not in settings
    with pytest.raises(ValueError):
        producer_config(" ")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadline_rejected(timeout):
    with pytest.raises(ValueError):
        publisher(ProducerStub(), timeout)
