import json
from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentState,
    ModelCandidate,
    ModelInput,
    NewDocumentProposal,
    Outcome,
    RelationGenerationScope,
    Usage,
)
from app.refresh.rules import RefreshRules
from app.refresh.service import RefreshService
from tests.fakes.real_rules import real_example
from tests.fakes.refresh import (
    Calls,
    FakeDocuments,
    FakeInbox,
    FakeJsonArtifacts,
    FakeModel,
    FakeRelations,
)
from tests.refresh.test_context import snapshot_fixture
from tests.refresh.test_relation_ownership import relation_between


def separate_snapshot(**settings):
    snapshot = snapshot_fixture()
    return replace(
        snapshot,
        request=replace(
            snapshot.request,
            settings=replace(
                snapshot.request.settings, separate_discovery=True, **settings
            ),
        ),
    )


def discovery_input():
    return ModelInput(
        "fixed",
        "v3",
        "v6",
        "fake",
        (),
        allow_new_documents=True,
        relation_scope=RelationGenerationScope((), True),
        allow_document_updates=False,
    )


def update_input(target):
    return ModelInput(
        "fixed",
        "v3",
        "v6",
        "fake",
        (target,),
        allow_new_documents=False,
        relation_scope=RelationGenerationScope((target,), False),
        allow_document_updates=True,
    )


def discovery_candidate(snapshot):
    evidence = real_example()[-1].document_proposals[0].evidence
    created = NewDocumentProposal(
        "new:CHARACTER:윤아", "CHARACTER", "윤아", "변경 원고에서 확인한 인물", evidence
    )
    relation = replace(
        relation_between(snapshot, "c1", "m1"),
        document_id=created.candidate_id,
        base_revision_no=None,
    )
    return ModelCandidate((), Usage(1, 100, 50), (relation,), (created,))


def test_discovery_and_update_inputs_have_separate_capabilities():
    snapshot = separate_snapshot()
    rules = RefreshRules()
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert [value.target_ids for value in inputs] == [(), ("c1",), ("i1",)]
    assert [value.allow_new_documents for value in inputs] == [True, False, False]
    assert [value.allow_document_updates for value in inputs] == [False, True, True]
    assert [value.relation_scope for value in inputs] == [
        RelationGenerationScope((), True),
        RelationGenerationScope(("c1",), False),
        RelationGenerationScope(("i1",), False),
    ]
    for value in inputs:
        assert (
            json.loads(value.prompt)["allow_document_updates"]
            == value.allow_document_updates
        )


def test_default_policy_preserves_original_target_call_count_and_capabilities():
    snapshot = snapshot_fixture()
    rules = RefreshRules()
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert not snapshot.request.settings.separate_discovery
    assert [value.target_ids for value in inputs] == [("c1",), ("i1",)]
    assert [value.allow_document_updates for value in inputs] == [True, True]
    assert [value.allow_new_documents for value in inputs] == [True, False]


def test_no_targets_requires_only_one_readonly_discovery_call():
    snapshot = replace(separate_snapshot(), target_ids=())
    rules = RefreshRules()
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert len(inputs) == 1
    assert inputs[0].target_ids == ()
    assert not inputs[0].allow_document_updates
    result = rules.validate_candidate(
        snapshot, discovery_candidate(snapshot), inputs[0]
    )
    assert result.outcome == Outcome.PROPOSED
    assert len(result.new_document_proposals) == len(result.relation_proposals) == 1


def test_inactive_changes_do_not_trigger_discovery():
    snapshot = separate_snapshot()
    snapshot = replace(
        snapshot,
        documents=tuple(
            replace(document, state=DocumentState.DELETED, body_text=None)
            if document.document_id in snapshot.changed_ids
            else document
            for document in snapshot.documents
        ),
    )
    assert RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot)) == ()


def test_discovery_call_counts_toward_budget_before_any_model_call():
    snapshot = separate_snapshot(max_model_calls=2)
    with pytest.raises(RefreshFailure) as caught:
        RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot))
    assert caught.value.failure.code == "CALL_BUDGET_EXCEEDED"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_separate_discovery_flag_must_be_boolean(value):
    snapshot = snapshot_fixture()
    snapshot = replace(
        snapshot,
        request=replace(
            snapshot.request,
            settings=replace(snapshot.request.settings, separate_discovery=value),
        ),
    )
    with pytest.raises(RefreshFailure) as caught:
        RefreshRules().validate_input(snapshot.request, real_example()[1])
    assert caught.value.failure.code == "INVALID_POLICY"
    with pytest.raises(RefreshFailure) as caught:
        RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot))
    assert caught.value.failure.code == "INVALID_POLICY"


def test_empty_discovery_targets_never_expand_to_all_setting_updates():
    snapshot = separate_snapshot()
    valid_update = real_example()[-1].document_proposals[0]
    result = RefreshRules().validate_candidate(
        snapshot, ModelCandidate((valid_update,)), discovery_input()
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_TARGET"
    assert result.document_proposals == ()


def test_discovery_owns_new_and_nontarget_relations_but_not_target_pairs():
    snapshot = separate_snapshot()
    rules = RefreshRules()
    assert (
        rules.validate_candidate(
            snapshot, discovery_candidate(snapshot), discovery_input()
        ).outcome
        == Outcome.PROPOSED
    )
    no_target_pair = relation_between(snapshot, "m1", "m2")
    assert (
        rules.validate_candidate(
            snapshot,
            ModelCandidate((), relation_proposals=(no_target_pair,)),
            discovery_input(),
        ).outcome
        == Outcome.PROPOSED
    )
    target_pair = relation_between(snapshot, "c1", "i1")
    rejected = rules.validate_candidate(
        snapshot,
        ModelCandidate((), relation_proposals=(target_pair,)),
        discovery_input(),
    )
    assert rejected.failure.code == "INVALID_RELATION_SCOPE"


def test_first_setting_call_has_target_ownership_without_fallback():
    snapshot = separate_snapshot()
    original = real_example()[-1]
    candidate = replace(original, document_proposals=(original.document_proposals[0],))
    accepted = RefreshRules().validate_candidate(
        snapshot, candidate, update_input("c1")
    )
    assert accepted.outcome == Outcome.PROPOSED
    fallback_pair = relation_between(snapshot, "m1", "m2")
    rejected = RefreshRules().validate_candidate(
        snapshot,
        replace(candidate, relation_proposals=(fallback_pair,)),
        update_input("c1"),
    )
    assert rejected.failure.code == "INVALID_RELATION_SCOPE"
    assert rejected.document_proposals == ()


@pytest.mark.parametrize(
    "model_input",
    [
        replace(discovery_input(), allow_document_updates=True),
        replace(discovery_input(), allow_new_documents=False),
        replace(
            discovery_input(),
            target_ids=("c1",),
            relation_scope=RelationGenerationScope(("c1",), True),
        ),
        replace(
            update_input("c1"), relation_scope=RelationGenerationScope(("c1",), True)
        ),
        replace(update_input("c1"), allow_new_documents=True),
    ],
)
def test_phase_scope_metadata_cannot_grant_other_phase_capabilities(model_input):
    result = RefreshRules().validate_candidate(
        separate_snapshot(), ModelCandidate(()), model_input
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_RELATION_SCOPE"


def service_scenario(*, bad_update=False, call_budget=3):
    request, source, related, targets, expected = real_example()
    request = replace(
        request,
        settings=replace(
            request.settings, separate_discovery=True, max_model_calls=call_budget
        ),
    )
    snapshot = separate_snapshot()
    discovery = discovery_candidate(snapshot)
    first_update = replace(
        expected, document_proposals=(expected.document_proposals[0],)
    )
    second_update = replace(
        expected,
        document_proposals=(expected.document_proposals[1],),
        relation_proposals=(),
    )
    if bad_update:
        first_update = replace(
            first_update,
            document_proposals=(
                replace(expected.document_proposals[0], base_revision_no=99),
            ),
        )
    calls = Calls()
    artifacts = FakeJsonArtifacts({request.input_ref: source}, calls)
    model = FakeModel(
        ModelCandidate(()), calls, (discovery, first_update, second_update)
    )
    service = RefreshService(
        artifacts=artifacts,
        relations=FakeRelations(related, calls),
        documents=FakeDocuments(targets, calls),
        model=model,
        inbox=FakeInbox(calls=calls),
    )
    return request, service, artifacts, calls


def test_service_aggregate_restores_update_capability_after_readonly_discovery():
    request, service, artifacts, calls = service_scenario()
    done = service.run(request)
    result = artifacts.read_result(request)
    assert done.outcome == Outcome.PROPOSED
    assert len(result.document_proposals) == 2
    assert len(result.new_document_proposals) == 1
    assert len(result.relation_proposals) == 2
    assert result.usage.calls == 3
    assert service.run(request) == done
    assert calls.names().count("model.generate") == 3


def test_bad_update_after_discovery_preserves_atomic_failure():
    request, service, artifacts, calls = service_scenario(bad_update=True)
    done = service.run(request)
    result = artifacts.read_result(request)
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == "REVISION_MISMATCH"
    assert (
        result.document_proposals
        == result.new_document_proposals
        == result.relation_proposals
        == ()
    )
    assert calls.names().count("model.generate") == 2


def test_service_rejects_insufficient_budget_before_discovery():
    request, service, _, calls = service_scenario(call_budget=2)
    done = service.run(request)
    assert done.failure.code == "CALL_BUDGET_EXCEEDED"
    assert "model.generate" not in calls.names()


@pytest.mark.parametrize("value", ["true", 1, None])
def test_invalid_discovery_flag_fails_before_fetch_snapshot_or_model(value):
    request, service, _, calls = service_scenario()
    request = replace(
        request, settings=replace(request.settings, separate_discovery=value)
    )
    done = service.run(request)
    assert done.outcome == Outcome.FAILED
    assert done.failure.code == "INVALID_POLICY"
    assert "documents.fetch_documents" not in calls.names()
    assert "artifacts.save_context" not in calls.names()
    assert "model.generate" not in calls.names()
