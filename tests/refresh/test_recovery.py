"""Orchestration evidence only: rules are scripted and storage is in memory."""

from dataclasses import replace

import pytest

from app.refresh.errors import JobBusy, RefreshFailure, RequestConflict, RulesNotImplemented
from app.refresh.models import Failure, JobState, Outcome, Readiness
from tests.fakes.refresh import FakeJobs
from tests.fakes.scenario import scenario


def failure(code="TEST_TIMEOUT", retryable=True):
    return RefreshFailure(Failure(code, code, retryable))


@pytest.mark.parametrize("operation", [
    "artifacts.read_input", "relations.find_related", "documents.fetch_documents",
    "artifacts.save_context",
])
def test_input_io_failure_prevents_model_and_releases_for_delivery_retry(operation):
    s = scenario()
    s.calls.failures[operation].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request, "execution-1")
    assert "model.generate" not in s.calls.names()
    assert s.publisher.delivered == []
    assert s.jobs.load(s.request).state == JobState.RETRYABLE


@pytest.mark.parametrize("readiness", [Readiness.WAITING, Readiness.UNKNOWN])
def test_graph_delay_is_not_an_empty_success(readiness):
    s = scenario()
    s.relations.response = replace(s.relations.response, readiness=readiness)
    with pytest.raises(RefreshFailure) as error:
        s.service().run(s.request, "execution-1")
    assert error.value.failure.code == "GRAPH_NOT_READY"
    assert "documents.fetch_documents" not in s.calls.names()
    assert "model.generate" not in s.calls.names()
    assert s.publisher.delivered == []


@pytest.mark.parametrize("operation,code", [
    ("rules.validate_input", "WRONG_PROJECT"),
    ("documents.fetch_documents", "ACCESS_DENIED"),
    ("rules.assemble", "REQUIRED_DOCUMENT_MISSING"),
    ("rules.assemble", "DOCUMENT_DELETED"),
])
def test_terminal_rejection_is_failed_completion_not_success(operation, code):
    s = scenario()
    s.calls.failures[operation].append(failure(code, False))
    done = s.service().run(s.request, "execution-1")
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == code
    assert s.artifacts.read_result(s.request).proposals == ()
    assert "model.generate" not in s.calls.names()
    assert s.jobs.load(s.request).state == JobState.COMPLETED


@pytest.mark.parametrize("code", ["BAD_FIELD", "MISSING_EVIDENCE"])
def test_candidate_validation_rejection_does_not_publish_proposal(code):
    s = scenario()
    s.calls.failures["rules.validate_candidate"].append(failure(code, False))
    done = s.service().run(s.request, "execution-1")
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == code
    assert s.artifacts.read_result(s.request).proposals == ()
    assert len(s.publisher.delivered) == 1


def test_completed_duplicate_and_conflicting_input_do_not_generate_again():
    s = scenario()
    first = s.service().run(s.request, "execution-1")
    assert s.service().run(s.request, "execution-2") == first
    assert s.calls.names().count("model.generate") == 1
    assert s.publisher.delivered == [first]
    with pytest.raises(RequestConflict):
        s.service().run(replace(s.request, required_graph_version="changed"), "execution-3")
    assert s.jobs.load(s.request).state == JobState.COMPLETED


def test_busy_request_does_not_release_another_execution():
    s = scenario()
    owner = s.jobs.claim(s.request, "owner")
    with pytest.raises(JobBusy):
        s.service().run(s.request, "contender")
    assert s.jobs.load(s.request) == owner
    assert "artifacts.read_input" not in s.calls.names()


def test_model_retry_reuses_frozen_context_despite_source_changes():
    s = scenario()
    s.calls.failures["model.generate"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request, "execution-1")
    fixed = s.artifacts.read_context(s.request)
    s.artifacts.inputs.clear()
    s.documents.documents.clear()
    s.relations.response = replace(s.relations.response, readiness=Readiness.WAITING)
    # Recreate service and job store to exercise checkpoint-based recovery, not a DB restart.
    s.jobs = FakeJobs((s.jobs.load(s.request),), s.calls)
    done = s.service().run(s.request, "execution-2")
    assert done.outcome == Outcome.PROPOSED
    assert s.artifacts.read_context(s.request) == fixed
    assert s.calls.names().count("artifacts.read_input") == 1
    assert s.calls.names().count("relations.find_related") == 1
    assert s.calls.names().count("documents.fetch_documents") == 1
    assert s.calls.names().count("model.generate") == 2


@pytest.mark.parametrize("operation,prefix,delivered_before", [
    ("publisher.publish", [], 0),
    ("publisher.ack", [], 1),
    ("jobs.checkpoint", [None], 0),  # context saved; result saved; checkpoint fails
    ("jobs.checkpoint", [None, None], 1),  # delivered; completion checkpoint fails
])
def test_saved_result_recovers_without_regeneration(operation, prefix, delivered_before):
    s = scenario()
    s.calls.failures[operation].extend([*prefix, failure()])
    with pytest.raises(RefreshFailure):
        s.service().run(s.request, "execution-1")
    assert s.artifacts.read_result(s.request) == s.result
    assert len(s.publisher.delivered) == delivered_before
    s.artifacts.inputs.clear()
    s.documents.documents.clear()
    s.jobs = FakeJobs((s.jobs.load(s.request),), s.calls)
    done = s.service().run(s.request, "execution-2")
    assert s.calls.names().count("model.generate") == 1
    assert s.calls.names().count("documents.fetch_documents") == 1
    assert s.jobs.load(s.request).state == JobState.COMPLETED
    assert len(s.publisher.delivered) == delivered_before + 1
    assert all(item == done for item in s.publisher.delivered)


def test_context_saved_before_checkpoint_failure_is_reused():
    s = scenario()
    s.calls.failures["jobs.checkpoint"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request, "execution-1")
    assert "model.generate" not in s.calls.names()
    assert s.jobs.load(s.request).context_ref is None
    s.service().run(s.request, "execution-2")
    assert s.calls.names().count("artifacts.read_input") == 1
    assert s.calls.names().count("model.generate") == 1


def test_result_save_failure_can_require_another_model_call():
    s = scenario()
    s.calls.failures["artifacts.save_result"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request, "execution-1")
    assert s.artifacts.read_result(s.request) is None
    assert s.publisher.delivered == []
    s.service().run(s.request, "execution-2")
    assert s.calls.names().count("model.generate") == 2
    assert s.calls.names().count("artifacts.read_input") == 1


def test_lost_saved_context_does_not_silently_refetch_current_documents():
    s = scenario()
    s.calls.failures["model.generate"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request, "execution-1")
    s.artifacts.contexts.clear()
    done = s.service().run(s.request, "execution-2")
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == "CONTEXT_MISSING"
    assert s.calls.names().count("artifacts.read_input") == 1
    assert s.calls.names().count("model.generate") == 1


def test_lost_completed_result_does_not_regenerate_or_publish():
    s = scenario()
    s.service().run(s.request, "execution-1")
    s.artifacts.results.clear()
    with pytest.raises(RefreshFailure, match="Completed result unavailable"):
        s.service().run(s.request, "execution-2")
    assert s.calls.names().count("model.generate") == 1
    assert len(s.publisher.delivered) == 1
    assert s.jobs.load(s.request).state == JobState.COMPLETED


@pytest.mark.parametrize("operation", ["rules.select", "rules.chunk", "rules.validate_candidate"])
def test_unimplemented_steps_are_not_converted_to_normal_results(operation):
    s = scenario()
    s.calls.failures[operation].append(RulesNotImplemented(operation))
    with pytest.raises(RulesNotImplemented):
        s.service().run(s.request, "execution-1")
    assert s.artifacts.read_result(s.request) is None
    assert s.publisher.delivered == []


def test_release_failure_preserves_original_error_and_requires_lease_recovery():
    s = scenario()
    s.calls.failures["artifacts.read_input"].append(RuntimeError("unexpected defect"))
    s.calls.failures["jobs.checkpoint"].append(failure())
    with pytest.raises(RuntimeError, match="unexpected defect") as error:
        s.service().run(s.request, "execution-1")
    assert "Job release failed: RefreshFailure" in error.value.__notes__
    assert s.jobs.load(s.request).state == JobState.RUNNING
    assert s.publisher.delivered == []
    with pytest.raises(JobBusy):
        s.service().run(s.request, "execution-2")
