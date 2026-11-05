"""Outbox 전송 실패·확인 유실·재시작·종료에서 완료 기록과 재발행을 검증한다."""

from threading import Event

import pytest

from app.kafka.errors import DeliveryError
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure
from app.refresh.outbox import RefreshOutboxRelay
from tests.fakes.refresh import FakeInbox
from tests.fakes.scenario import scenario


@pytest.mark.parametrize(
    "operation,delivered",
    [("publisher.publish", 0), ("publisher.ack", 1), ("outbox.mark_sent", 1)],
)
def test_pending_survives_delivery_failure_and_service_restart(operation, delivered):
    s = scenario()
    done = s.service().run(s.request)
    assert s.inbox.find(s.request) == done
    assert not s.publisher.delivered
    entry = s.inbox.pending()[0]
    s.calls.failures[operation].append(DeliveryError("unconfirmed"))
    with pytest.raises(DeliveryError):
        RefreshOutboxRelay(s.inbox, s.publisher).drain(Event())
    assert len(s.publisher.delivered) == delivered
    assert s.inbox.pending() == [entry]
    s.inbox = FakeInbox(s.inbox.records, s.calls)
    s.artifacts.inputs.clear()
    s.artifacts.results.clear()
    assert s.service().run(s.request) == done
    assert s.calls.names().count("model.generate") == 1
    assert RefreshOutboxRelay(s.inbox, s.publisher).drain(Event()) == 1
    assert s.inbox.pending() == []
    assert all(item == done for item in s.publisher.delivered)
    # A duplicate request never re-arms a sent event.
    s.inbox.complete(s.request, done)
    assert RefreshOutboxRelay(s.inbox, s.publisher).drain(Event()) == 0


def test_mark_response_loss_is_recovered_as_sent():
    s = scenario()
    s.service().run(s.request)
    s.calls.failures["outbox.ack"].append(
        RefreshFailure(Failure("DB_TIMEOUT", "response lost", True))
    )
    with pytest.raises(RefreshFailure):
        RefreshOutboxRelay(s.inbox, s.publisher).drain(Event())
    assert not FakeInbox(s.inbox.records).pending()
    assert len(s.publisher.delivered) == 1


def test_relay_uses_stored_event_time_and_honors_stop():
    s = scenario()
    done = s.service().run(s.request)
    entry = s.inbox.pending()[0]
    seen = []

    class Publisher:
        def publish(self, completion, *, occurred_at):
            seen.append((completion, occurred_at))

    relay = RefreshOutboxRelay(s.inbox, Publisher())
    stop = Event()
    stop.set()
    assert relay.drain(stop) == 0
    assert s.inbox.pending() == [entry]
    assert relay.drain(Event()) == 1
    assert seen == [(done, entry.created_at)]


def test_background_retries_delivery_and_db_failures_without_new_request():
    s = scenario()
    done = s.service().run(s.request)
    s.calls.failures["publisher.publish"].append(DeliveryError("offline"))
    s.calls.failures["outbox.mark_sent"].append(
        RefreshFailure(Failure("DB_DOWN", "offline", True))
    )

    class Stop:
        waits = []

        def is_set(self):
            return not s.inbox.pending()

        def wait(self, seconds):
            self.waits.append(seconds)
            return self.is_set()

    stop = Stop()
    RefreshOutboxRelay(s.inbox, s.publisher).run(stop)
    assert stop.waits == [1, 2]
    assert s.publisher.delivered == [done, done]
    assert s.calls.names().count("model.generate") == 1
