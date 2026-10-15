from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import ArtifactRef, Completion, Failure, Outcome
from tests.fakes.fixtures import example
from tests.fakes.refresh import FakeInbox, FakePublisher


def test_inbox_only_records_completion_and_rejects_reused_request_inputs():
    request = example()[0]
    completion = Completion(request.job, ArtifactRef("b", "result"), Outcome.NO_CHANGE)
    inbox = FakeInbox()
    assert inbox.find(request) is None
    assert inbox.find(request) is None
    inbox.complete(request, completion)
    inbox.complete(request, completion)
    restored = FakeInbox(inbox.records)
    assert restored.find(request) == completion
    with pytest.raises(RequestConflict):
        restored.find(replace(request, discover_related=False))
    with pytest.raises(RequestConflict):
        restored.complete(request, replace(completion, outcome=Outcome.PROPOSED))
    assert FakeInbox().find(request) is None


@pytest.mark.parametrize(
    "operation,delivery_count", [("publisher.publish", 0), ("publisher.ack", 1)]
)
def test_publisher_distinguishes_failure_before_delivery_and_lost_ack(
    operation, delivery_count
):
    request = example()[0]
    completion = Completion(request.job, ArtifactRef("b", "result"), Outcome.NO_CHANGE)
    publisher = FakePublisher()
    publisher.calls.failures[operation].append(
        RefreshFailure(Failure("PUBLISH_TIMEOUT", "timeout", True))
    )
    with pytest.raises(RefreshFailure):
        publisher.publish(completion)
    assert len(publisher.delivered) == delivery_count
    publisher.publish(completion)
    assert len(publisher.delivered) == delivery_count + 1
