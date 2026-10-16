"""완료 Inbox와 불변 S3 결과·문맥을 이용한 재전달 복구를 검증한다."""

from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure, RequestConflict, RulesNotImplemented
from app.refresh.models import Failure, Outcome, Readiness
from tests.fakes.refresh import FakeInbox
from tests.fakes.scenario import scenario


def failure(code="TEST_TIMEOUT", retryable=True):
    return RefreshFailure(Failure(code, code, retryable))


@pytest.mark.parametrize(
    "operation",
    [
        "inbox.find",
        "artifacts.read_input",
        "relations.find_related",
        "documents.fetch_documents",
        "artifacts.save_context",
    ],
)
def test_input_io_failure_prevents_model_and_leaves_no_completed_inbox(operation):
    s = scenario()
    s.calls.failures[operation].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request)
    assert "model.generate" not in s.calls.names()
    assert s.publisher.delivered == []
    assert s.inbox.find(s.request) is None


@pytest.mark.parametrize("readiness", [Readiness.WAITING, Readiness.UNKNOWN])
def test_graph_delay_is_not_an_empty_success(readiness):
    s = scenario()
    s.relations.response = replace(s.relations.response, readiness=readiness)
    with pytest.raises(RefreshFailure) as error:
        s.service().run(s.request)
    assert error.value.failure.code == "GRAPH_NOT_READY"
    assert "documents.fetch_documents" not in s.calls.names()
    assert "model.generate" not in s.calls.names()
    assert s.inbox.find(s.request) is None


@pytest.mark.parametrize(
    "operation,code",
    [
        ("rules.validate_input", "WRONG_PROJECT"),
        ("documents.fetch_documents", "ACCESS_DENIED"),
        ("rules.assemble", "REQUIRED_DOCUMENT_MISSING"),
        ("rules.assemble", "DOCUMENT_DELETED"),
    ],
)
def test_terminal_rejection_is_failed_completion_not_success(operation, code):
    s = scenario()
    s.calls.failures[operation].append(failure(code, False))
    done = s.service().run(s.request)
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == code
    assert s.artifacts.read_result(s.request).document_proposals == ()
    assert "model.generate" not in s.calls.names()
    assert s.inbox.find(s.request) == done


@pytest.mark.parametrize("code", ["BAD_FIELD", "MISSING_EVIDENCE"])
def test_candidate_validation_rejection_does_not_publish_proposal(code):
    s = scenario()
    s.calls.failures["rules.validate_candidate"].append(failure(code, False))
    done = s.service().run(s.request)
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == code
    assert s.artifacts.read_result(s.request).document_proposals == ()
    assert s.inbox.find(s.request) == done


def test_completed_duplicate_and_conflicting_input_do_not_generate_again():
    s = scenario()
    first = s.service().run(s.request)
    s.calls.events.clear()
    assert s.service().run(s.request) == first
    assert s.calls.names() == ["inbox.find"]
    assert [entry.completion for entry in s.inbox.pending()] == [first]
    with pytest.raises(RequestConflict):
        s.service().run(replace(s.request, required_graph_version="changed"))
    assert s.inbox.find(s.request) == first


def test_model_retry_reuses_frozen_context_despite_source_changes():
    s = scenario()
    s.calls.failures["model.generate"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request)
    fixed = s.artifacts.read_context(s.request)
    s.artifacts.inputs.clear()
    s.documents.documents.clear()
    s.relations.response = replace(s.relations.response, readiness=Readiness.WAITING)
    s.inbox = FakeInbox(calls=s.calls)
    done = s.service().run(s.request)
    assert done.outcome == Outcome.PROPOSED
    assert s.artifacts.read_context(s.request) == fixed
    for operation in (
        "artifacts.read_input",
        "relations.find_related",
        "documents.fetch_documents",
    ):
        assert s.calls.names().count(operation) == 1
    assert s.calls.names().count("model.generate") == 2


@pytest.mark.parametrize("operation", ["inbox.complete", "inbox.ack"])
def test_saved_result_recovers_without_regeneration(operation):
    s = scenario()
    s.calls.failures[operation].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request)
    stored = s.artifacts.read_result(s.request)
    assert replace(stored, attempts=()) == s.result
    assert len(stored.attempts) == 1
    assert s.publisher.delivered == []
    s.artifacts.inputs.clear()
    s.documents.documents.clear()
    s.inbox = FakeInbox(s.inbox.records, s.calls)
    done = s.service().run(s.request)
    assert s.calls.names().count("model.generate") == 1
    assert s.calls.names().count("documents.fetch_documents") == 1
    assert s.inbox.find(s.request) == done
    assert len(s.inbox.pending()) == 1
    assert s.publisher.delivered == []
    assert s.inbox.pending()[0].completion == done


def test_result_save_failure_can_require_another_model_call():
    s = scenario()
    s.calls.failures["artifacts.save_result"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request)
    assert s.artifacts.read_result(s.request) is None
    assert s.inbox.find(s.request) is None
    s.service().run(s.request)
    assert s.calls.names().count("model.generate") == 2
    assert s.calls.names().count("artifacts.read_input") == 1


def test_missing_uncompleted_context_can_restart_from_input():
    s = scenario()
    s.calls.failures["model.generate"].append(failure())
    with pytest.raises(RefreshFailure):
        s.service().run(s.request)
    s.artifacts.contexts.clear()
    assert s.service().run(s.request).outcome == Outcome.PROPOSED
    assert s.calls.names().count("artifacts.read_input") == 2
    assert s.calls.names().count("model.generate") == 2


def test_completed_inbox_skips_io_even_when_old_artifacts_are_gone():
    s = scenario()
    first = s.service().run(s.request)
    s.artifacts.results.clear()
    s.artifacts.contexts.clear()
    s.artifacts.inputs.clear()
    s.calls.events.clear()
    assert s.service().run(s.request) == first
    assert s.calls.names() == ["inbox.find"]
    assert [entry.completion for entry in s.inbox.pending()] == [first]


@pytest.mark.parametrize(
    "operation", ["rules.select", "rules.chunk", "rules.validate_candidate"]
)
def test_unimplemented_steps_are_not_converted_to_normal_results(operation):
    s = scenario()
    s.calls.failures[operation].append(RulesNotImplemented(operation))
    with pytest.raises(RulesNotImplemented):
        s.service().run(s.request)
    assert s.artifacts.read_result(s.request) is None
    assert s.publisher.delivered == []
    assert s.inbox.find(s.request) is None


def test_unexpected_failure_has_no_lease_to_release_before_redelivery():
    s = scenario()
    s.calls.failures["artifacts.read_input"].append(RuntimeError("unexpected defect"))
    with pytest.raises(RuntimeError, match="unexpected defect"):
        s.service().run(s.request)
    assert s.inbox.find(s.request) is None
    assert s.service().run(s.request).outcome == Outcome.PROPOSED


def test_concurrent_result_winner_is_reused_instead_of_overwritten():
    s = scenario()
    save = s.artifacts.save_result
    winner = replace(s.result, outcome=Outcome.NO_CHANGE, document_proposals=())
    raced = False

    def save_after_race(request, result):
        nonlocal raced
        if not raced:
            raced = True
            save(request, winner)
            raise RequestConflict("another execution saved its result")
        return save(request, result)

    s.artifacts.save_result = save_after_race
    completion = s.service().run(s.request)
    assert completion.outcome == Outcome.NO_CHANGE
    assert s.artifacts.read_result(s.request) == winner
    assert s.inbox.find(s.request) == completion
