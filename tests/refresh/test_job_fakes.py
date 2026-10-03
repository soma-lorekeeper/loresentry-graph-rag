from dataclasses import replace

import pytest

from app.refresh.errors import JobBusy, RefreshFailure, RequestConflict
from app.refresh.models import ArtifactRef, Completion, Failure, JobState, Outcome
from tests.fakes.fixtures import example
from tests.fakes.refresh import FakeJobs, FakePublisher


def test_claim_rejects_duplicates_and_reused_request_inputs():
    request = example()[0]
    jobs = FakeJobs()
    record = jobs.claim(request, "execution-1")
    with pytest.raises(JobBusy):
        jobs.claim(request, "execution-1")
    with pytest.raises(JobBusy):
        jobs.claim(request, "execution-2")
    with pytest.raises(RequestConflict):
        jobs.claim(replace(request, discover_related=False), "execution-2")
    jobs.checkpoint(replace(record, state=JobState.RETRYABLE))
    newer = jobs.claim(request, "execution-2")
    with pytest.raises(JobBusy):
        jobs.checkpoint(record)
    assert newer.execution_id == "execution-2"
    assert FakeJobs().load(request) is None


def test_checkpoint_records_context_result_and_terminal_state_for_resume():
    request = example()[0]
    jobs = FakeJobs()
    record = jobs.claim(request, "execution-1")
    context = ArtifactRef("b", "context")
    result = ArtifactRef("b", "result")
    record = replace(record, context_ref=context)
    jobs.checkpoint(record)
    with pytest.raises(ValueError, match="result reference"):
        jobs.checkpoint(replace(record, state=JobState.RESULT_READY))
    record = replace(record, state=JobState.RESULT_READY, result_ref=result)
    jobs.checkpoint(record)
    jobs.checkpoint(replace(record, state=JobState.RETRYABLE))
    restored = FakeJobs((jobs.load(request),))
    resumed = restored.claim(request, "execution-2")
    assert resumed.context_ref == context and resumed.result_ref == result
    with pytest.raises(RequestConflict):
        restored.checkpoint(replace(resumed, result_ref=ArtifactRef("b", "wrong")))
    restored.checkpoint(replace(resumed, state=JobState.RESULT_READY))
    done = replace(resumed, state=JobState.COMPLETED)
    restored.checkpoint(done)
    assert restored.claim(request, "execution-3") == done
    with pytest.raises(ValueError, match="invalid job transition"):
        restored.checkpoint(replace(done, state=JobState.RUNNING))


@pytest.mark.parametrize("operation,delivery_count", [("publisher.publish", 0), ("publisher.ack", 1)])
def test_publisher_distinguishes_failure_before_delivery_and_lost_ack(operation, delivery_count):
    request = example()[0]
    completion = Completion(request.job, ArtifactRef("b", "result"), Outcome.NO_CHANGE)
    publisher = FakePublisher()
    publisher.calls.failures[operation].append(RefreshFailure(Failure("PUBLISH_TIMEOUT", "timeout", True)))
    with pytest.raises(RefreshFailure):
        publisher.publish(completion)
    assert len(publisher.delivered) == delivery_count
    publisher.publish(completion)
    assert len(publisher.delivered) == delivery_count + 1
    assert FakePublisher().delivered == []
