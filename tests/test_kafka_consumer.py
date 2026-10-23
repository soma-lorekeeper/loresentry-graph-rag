"""업무 handler의 성공·재시도·DLQ 실패·종료 시 원본을 진행하지 않는지 확인한다."""

from dataclasses import replace
from threading import Event

import pytest

from app.kafka.consumer import BusinessConsumerHandler
from app.kafka.errors import (
    DeliveryError,
    InvalidEvent,
    NotReady,
    ProcessingStopped,
    RetryableProcessingError,
)
from app.kafka.messages import REFRESH_REQUESTED
from tests.test_kafka_messages import content_record


class DeadLetters:
    def __init__(self):
        self.sent = []
        self.failure = None

    def dead_letter(self, record, failure):
        if self.failure:
            raise self.failure
        self.sent.append((record, failure))


def handler(work, dead_letters=None, stop=None, retry_delays=(0, 0)):
    return BusinessConsumerHandler(
        REFRESH_REQUESTED,
        {"GraphRefreshRequested": work},
        dead_letters if dead_letters is not None else DeadLetters(),
        stop if stop is not None else Event(),
        retry_delays=retry_delays,
    )


def test_success_and_unknown_type_have_no_dead_letters(caplog):
    received = []
    dead_letters = DeadLetters()
    consume = handler(received.append, dead_letters)
    record = content_record()
    consume(record)
    assert len(received) == 1
    consume(replace(record, headers=(("ce_type", b"untrusted-secret"),)))
    assert len(received) == 1
    assert not dead_letters.sent
    assert "untrusted-secret" not in caplog.text


def test_invalid_wire_goes_to_dlq_without_calling_business():
    dead_letters = DeadLetters()
    consume = handler(
        lambda _: pytest.fail("invalid message reached business"), dead_letters
    )
    consume(replace(content_record(), value=b"not JSON"))
    assert len(dead_letters.sent) == 1
    assert isinstance(dead_letters.sent[0][1], InvalidEvent)


def test_transient_failure_retries_same_event_then_succeeds():
    received = []
    dead_letters = DeadLetters()

    def work(event):
        received.append(event)
        if len(received) < 3:
            raise RetryableProcessingError("secret")

    handler(work, dead_letters)(content_record())
    assert len(received) == 3
    assert all(event is received[0] for event in received)
    assert not dead_letters.sent


def test_retry_exhaustion_dead_letters_once_with_safe_logs(caplog):
    calls = []
    dead_letters = DeadLetters()

    def work(event):
        calls.append(event)
        raise RetryableProcessingError("secret manuscript")

    handler(work, dead_letters)(content_record())
    assert len(calls) == 3
    assert len(dead_letters.sent) == 1
    assert "secret manuscript" not in caplog.text


class WaitingWork:
    """투영 대기로 늘 실패하는 처리기. exhausted가 있으면 소진 뒤 그것을 부른다."""

    def __init__(self, hook_failure=None):
        self.calls, self.exhausted_calls = [], []
        self.hook_failure = hook_failure

    def __call__(self, event):
        self.calls.append(event)
        raise NotReady("PROJECTION_NOT_READY", "waiting")

    def exhausted(self, event, failure):
        self.exhausted_calls.append((event, failure.code))
        if self.hook_failure is not None:
            raise self.hook_failure


def test_projection_wait_exhaustion_is_handled_by_the_business_handler():
    dead_letters = DeadLetters()
    work = WaitingWork()
    handler(work, dead_letters)(content_record())
    assert len(work.calls) == 3
    assert [code for _, code in work.exhausted_calls] == ["PROJECTION_NOT_READY"]
    assert not dead_letters.sent


@pytest.mark.parametrize(
    "hook_failure",
    [RetryableProcessingError("store down"), InvalidEvent("conflict:refresh")],
)
def test_failed_exhaustion_handling_goes_to_dlq(hook_failure):
    dead_letters = DeadLetters()
    work = WaitingWork(hook_failure)
    handler(work, dead_letters)(content_record())
    assert len(work.exhausted_calls) == 1
    assert [failure for _, failure in dead_letters.sent] == [hook_failure]


def test_other_retryable_failures_still_go_to_dlq_after_retries():
    dead_letters = DeadLetters()
    work = WaitingWork()

    def unavailable(event):
        work.calls.append(event)
        raise RetryableProcessingError("model down")

    unavailable.exhausted = work.exhausted
    handler(unavailable, dead_letters)(content_record())
    assert work.exhausted_calls == []
    assert len(dead_letters.sent) == 1


@pytest.mark.parametrize(
    "failure", [RuntimeError("bug"), DeliveryError("uncertain ack")]
)
def test_unclassified_or_transport_failure_propagates_without_dlq(failure):
    dead_letters = DeadLetters()

    def work(event):
        raise failure

    with pytest.raises(type(failure)):
        handler(work, dead_letters)(content_record())
    assert not dead_letters.sent


def test_dlq_failure_propagates():
    dead_letters = DeadLetters()
    dead_letters.failure = DeliveryError("unavailable")
    with pytest.raises(DeliveryError):
        handler(lambda _: None, dead_letters)(replace(content_record(), value=None))


def test_shutdown_interrupts_retry_before_dlq():
    stop = Event()
    dead_letters = DeadLetters()

    def work(event):
        stop.set()
        raise RetryableProcessingError("temporarily unavailable")

    with pytest.raises(ProcessingStopped):
        handler(work, dead_letters, stop, retry_delays=(10,))(content_record())
    assert not dead_letters.sent


def test_missing_business_handler_fails_at_composition():
    with pytest.raises(ValueError, match="explicit handler"):
        BusinessConsumerHandler(REFRESH_REQUESTED, {}, DeadLetters(), Event())


@pytest.mark.parametrize(
    "failure_kind", ["none", "retry", "invalid", "dlq", "delivery", "bug"]
)
def test_polling_commits_only_confirmed_business_or_dead_letter(failure_kind):
    from app.kafka.consumer import business_worker
    from tests.test_kafka_polling import Message, Transport

    original = content_record()

    class WireMessage(Message):
        def topic(self):
            return original.topic

        def key(self):
            return original.key

        def value(self):
            return original.value

        def headers(self):
            return list(original.headers)

    stop = Event()
    transport = Transport(stop, [WireMessage(), WireMessage(1)])
    dead_letters = DeadLetters()
    if failure_kind == "dlq":
        dead_letters.failure = DeliveryError("failed DLQ")
    calls = []

    def work(event):
        calls.append(event)
        if failure_kind == "none":
            return
        if failure_kind in {"invalid", "dlq"}:
            raise InvalidEvent("invalid:domain")
        if failure_kind == "retry":
            raise RetryableProcessingError("temporary")
        if failure_kind == "delivery":
            raise DeliveryError("failed completion")
        raise RuntimeError("programming error")

    runtime = business_worker(
        transport,
        REFRESH_REQUESTED,
        {"GraphRefreshRequested": work},
        dead_letters,
        stop,
        retry_delays=(0,),
    )
    if failure_kind in {"dlq", "delivery", "bug"}:
        with pytest.raises((DeliveryError, RuntimeError)):
            runtime.run(stop)
        assert not transport.commits
    else:
        runtime.run(stop)
        assert transport.commits == [(original.topic, 0, 1)]
    assert len(calls) == (2 if failure_kind == "retry" else 1)
    assert len(dead_letters.sent) == (1 if failure_kind in {"retry", "invalid"} else 0)
    assert transport.closed
