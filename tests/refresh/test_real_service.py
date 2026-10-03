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


@pytest.mark.parametrize(
    "count,chars,expected",
    [
        (20, 5000, None),
        (21, 5000, "TOO_MANY_CHANGED_DOCUMENTS"),
        (20, 5001, "BODY_TOO_LARGE"),
    ],
)
def test_full_service_input_boundaries(count, chars, expected):
    from tests.fakes.real_rules import changed_documents

    s = real_scenario()
    request, source = changed_documents(count, chars)
    s.artifacts.inputs[request.input_ref] = source
    s.model.responses.clear()
    before = repr(source)
    done = s.service.run(request, "run1")
    if expected is None:
        assert done.outcome == Outcome.NO_CHANGE
        assert s.calls.names().count("model.generate") == 2
    else:
        assert done.failure.code == expected
        assert "model.generate" not in s.calls.names()
    assert repr(source) == before


@pytest.mark.parametrize(
    "kind,code",
    [
        ("missing", "DOCUMENT_MISSING"),
        ("trashed", "DOCUMENT_INACTIVE"),
        ("deleted", "DOCUMENT_INACTIVE"),
        ("project", "PROJECT_MISMATCH"),
        ("manifest", "MANIFEST_MISMATCH"),
    ],
)
def test_full_service_rejects_unavailable_or_wrong_scope_data(kind, code):
    from app.refresh.models import DocumentState

    s = real_scenario()
    if kind == "missing":
        s.documents.documents.pop(("project", "c1"))
    elif kind in {"trashed", "deleted"}:
        key = ("project", "c1")
        s.documents.documents[key] = replace(
            s.documents.documents[key], state=DocumentState(kind.upper())
        )
    elif kind == "project":
        response = s.relations.response
        s.relations.response = replace(
            response, relations=(replace(response.relations[0], project_id="other"),)
        )
    else:
        source = s.source
        s.artifacts.inputs[s.request.input_ref] = replace(
            source,
            documents=(
                replace(source.documents[0], revision_no=99),
                source.documents[1],
            ),
        )
    done = s.service.run(s.request, "run1")
    assert done.failure.code == code
    assert "model.generate" not in s.calls.names()


def test_relation_only_without_setting_targets():
    from app.refresh.models import RelatedDocuments, RelationProposal

    s = real_scenario()
    s.relations.response = RelatedDocuments()
    s.model.responses.clear()
    evidence = s.candidate.document_proposals[0].evidence
    s.model.candidate = ModelCandidate(
        (),
        relation_proposals=(
            RelationProposal(
                "m1",
                "m2",
                3,
                5,
                "related_manuscript",
                "related_manuscript",
                "이어지는 사건",
                evidence,
            ),
        ),
    )
    done = s.service.run(s.request, "run1")
    assert done.outcome == Outcome.PROPOSED
    result = s.artifacts.read_result(s.request)
    assert result.document_proposals == ()
    assert len(result.relation_proposals) == 1
    assert result.usage.calls == 1
    assert "documents.fetch_documents" not in s.calls.names()


def test_all_inactive_changes_do_not_call_model():
    from app.refresh.models import DocumentState

    s = real_scenario()
    request = replace(
        s.request,
        changed_documents=tuple(
            replace(d, state=DocumentState.DELETED) for d in s.request.changed_documents
        ),
    )
    s.artifacts.inputs[request.input_ref] = replace(
        s.source,
        documents=tuple(
            replace(d, state=DocumentState.DELETED, body_text=None)
            for d in s.source.documents
        ),
    )
    done = s.service.run(request, "run1")
    assert done.outcome == Outcome.NO_CHANGE
    assert "model.generate" not in s.calls.names()


def test_wrong_target_in_first_call_fails_whole_request_and_stops():
    s = real_scenario()
    s.model.responses[0] = s.model.responses[1]
    done = s.service.run(s.request, "run1")
    assert done.failure.code == "INVALID_TARGET"
    assert s.calls.names().count("model.generate") == 1
    result = s.artifacts.read_result(s.request)
    assert result.document_proposals == result.relation_proposals == ()


def test_saved_result_storage_failure_has_no_premature_completion():
    s = real_scenario()
    s.calls.failures["artifacts.save_result"].append(
        RefreshFailure(Failure("STORAGE_TIMEOUT", "timeout", True))
    )
    with pytest.raises(RefreshFailure):
        s.service.run(s.request, "run1")
    assert s.publisher.delivered == []
    assert s.artifacts.json_results == {}
    assert s.artifacts.read_context(s.request) is not None


def test_corrupted_stored_json_does_not_regenerate():
    s = real_scenario()
    s.service.run(s.request, "run1")
    s.artifacts.json_results[("project", "request")] = "{broken"
    with pytest.raises(RefreshFailure) as caught:
        s.service.run(s.request, "run2")
    assert caught.value.failure.code == "RESULT_CORRUPTED"
    assert s.calls.names().count("model.generate") == 2
    assert len(s.publisher.delivered) == 1
