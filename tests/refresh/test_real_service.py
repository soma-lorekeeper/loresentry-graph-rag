"""실제 rules와 fake IO의 계약 통합 검증. 외부 서버나 실제 모델을 사용하지 않는다."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, ModelCandidate, Outcome
from app.refresh.service import RefreshService
from tests.fakes.real_rules import real_example
from tests.fakes.refresh import (
    Calls,
    FakeDocuments,
    FakeJobs,
    FakeJsonArtifacts,
    FakeModel,
    FakePublisher,
    FakeRelations,
)


def real_scenario():
    request, source, related, targets, candidate = real_example()
    calls = Calls()
    # Target calls sorted c1, i1. The same relation from both calls is deduplicated.
    responses = tuple(
        replace(candidate, document_proposals=(p,))
        for p in candidate.document_proposals
    )
    s = SimpleNamespace(
        request=request,
        source=source,
        candidate=candidate,
        calls=calls,
        artifacts=FakeJsonArtifacts({request.input_ref: source}, calls),
        relations=FakeRelations(related, calls),
        documents=FakeDocuments(targets, calls),
        model=FakeModel(ModelCandidate(()), calls, responses),
        jobs=FakeJobs(calls=calls),
        publisher=FakePublisher(calls),
    )
    s.service = RefreshService(
        **{
            name: getattr(s, name)
            for name in (
                "artifacts",
                "relations",
                "documents",
                "model",
                "jobs",
                "publisher",
            )
        }
    )
    return s


def test_multi_target_uses_real_rules_and_saves_before_publish():
    s = real_scenario()
    done = s.service.run(s.request, "run1")
    result = s.artifacts.read_result(s.request)
    assert done.outcome == Outcome.PROPOSED
    assert result.document_proposals == s.candidate.document_proposals
    assert len(result.relation_proposals) == 1
    assert result.usage.calls == 2
    assert result.usage.input_tokens == 200
    names = s.calls.names()
    assert names.index("artifacts.save_context") < names.index("model.generate")
    assert names.index("artifacts.save_result") < names.index("publisher.publish")
    assert s.service.run(s.request, "duplicate") == done
    assert s.calls.names().count("model.generate") == 2
    assert len(s.publisher.delivered) == 1


def test_call_budget_fails_before_any_model_call():
    s = real_scenario()
    request = replace(
        s.request, settings=replace(s.request.settings, max_model_calls=1)
    )
    done = s.service.run(request, "run1")
    assert done.failure.code == "CALL_BUDGET_EXCEEDED"
    assert "model.generate" not in s.calls.names()


def test_second_call_terminal_failure_never_returns_partial_proposals():
    s = real_scenario()
    s.model.responses[1] = RefreshFailure(
        Failure("MODEL_BAD_RESPONSE", "invalid response")
    )
    done = s.service.run(s.request, "run1")
    result = s.artifacts.read_result(s.request)
    assert done.outcome == Outcome.FAILED
    assert result.document_proposals == result.relation_proposals == ()
    assert result.usage.calls == 2


def test_retry_reuses_snapshot_and_repeats_calls_before_result_saved():
    s = real_scenario()
    original_responses = tuple(s.model.responses)
    s.model.responses[1] = RefreshFailure(Failure("TIMEOUT", "timeout", True))
    with pytest.raises(RefreshFailure):
        s.service.run(s.request, "run1")
    frozen = s.artifacts.read_context(s.request)
    assert s.artifacts.read_result(s.request) is None
    s.artifacts.inputs.clear()
    s.documents.documents.clear()
    s.model.responses.extend(original_responses)
    assert s.service.run(s.request, "run2").outcome == Outcome.PROPOSED
    assert s.artifacts.read_context(s.request) == frozen
    assert s.calls.names().count("model.generate") == 4
    assert s.calls.names().count("documents.fetch_documents") == 1


@pytest.mark.parametrize("operation", ["publisher.publish", "publisher.ack"])
def test_saved_result_retry_does_not_recall_model(operation):
    s = real_scenario()
    s.calls.failures[operation].append(
        RefreshFailure(Failure("TIMEOUT", "timeout", True))
    )
    with pytest.raises(RefreshFailure):
        s.service.run(s.request, "run1")
    result = s.artifacts.read_result(s.request)
    s.service.run(s.request, "run2")
    assert s.artifacts.read_result(s.request) == result
    assert s.calls.names().count("model.generate") == 2


def test_aggregate_conflicting_relation_descriptions_fails():
    s = real_scenario()
    second = s.model.responses[1]
    s.model.responses[1] = replace(
        second,
        relation_proposals=(
            replace(second.relation_proposals[0], description="충돌한 설명"),
        ),
    )
    done = s.service.run(s.request, "run1")
    assert done.failure.code == "CONFLICTING_PROPOSALS"
    assert s.artifacts.read_result(s.request).document_proposals == ()
