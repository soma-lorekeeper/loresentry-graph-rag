import json
from dataclasses import replace

import pytest

from app.refresh.models import (
    ModelCandidate,
    ModelInput,
    NewDocumentProposal,
    Outcome,
    RelationGenerationScope,
)
from app.refresh.policy import RELATION_KEYS
from app.refresh.rules import RefreshRules
from app.refresh.validation import relation_in_scope
from tests.fakes.real_rules import real_example
from tests.refresh.test_context import snapshot_fixture


def relation_between(snapshot, source_id, target_id):
    documents = {document.document_id: document for document in snapshot.documents}
    source, target = documents[source_id], documents[target_id]
    return replace(
        real_example()[-1].relation_proposals[0],
        document_id=source_id,
        target_document_id=target_id,
        base_revision_no=source.revision_no,
        target_base_revision_no=target.revision_no,
        relation_key=RELATION_KEYS[target.folder_code],
        reverse_relation_key=RELATION_KEYS[source.folder_code],
    )


def scoped_input(targets, fallback):
    return ModelInput(
        "fixed",
        "v3",
        "v5",
        "fake",
        targets,
        allow_new_documents=fallback,
        relation_scope=RelationGenerationScope(targets, fallback),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_existing_pair_has_one_owner_independent_of_direction(reverse):
    snapshot = snapshot_fixture()
    pair = ("i1", "c1") if reverse else ("c1", "i1")
    proposal = relation_between(snapshot, *pair)
    first = scoped_input(("c1",), True)
    second = scoped_input(("i1",), False)
    assert relation_in_scope(snapshot, proposal, first.relation_scope)
    assert not relation_in_scope(snapshot, proposal, second.relation_scope)
    candidate = ModelCandidate((), relation_proposals=(proposal,))
    assert (
        RefreshRules().validate_candidate(snapshot, candidate, first).outcome
        == Outcome.PROPOSED
    )
    rejected = RefreshRules().validate_candidate(snapshot, candidate, second)
    assert rejected.outcome == Outcome.FAILED
    assert rejected.failure.code == "INVALID_RELATION_SCOPE"
    assert rejected.relation_proposals == ()


def test_changed_manuscript_pair_is_owned_by_setting_target():
    snapshot = snapshot_fixture()
    proposal = relation_between(snapshot, "m1", "i1")
    assert not relation_in_scope(
        snapshot, proposal, RelationGenerationScope(("c1",), True)
    )
    assert relation_in_scope(
        snapshot, proposal, RelationGenerationScope(("i1",), False)
    )
    result = RefreshRules().validate_candidate(
        snapshot,
        ModelCandidate((), relation_proposals=(proposal,)),
        scoped_input(("i1",), False),
    )
    assert result.outcome == Outcome.PROPOSED


def test_pair_owner_follows_execution_target_order():
    snapshot = replace(snapshot_fixture(), target_ids=("i1", "c1"))
    proposal = relation_between(snapshot, "c1", "i1")
    assert relation_in_scope(snapshot, proposal, RelationGenerationScope(("i1",), True))
    assert not relation_in_scope(
        snapshot, proposal, RelationGenerationScope(("c1",), False)
    )


def test_pair_without_target_endpoints_uses_fallback_owner():
    snapshot = snapshot_fixture()
    proposal = relation_between(snapshot, "m1", "m2")
    assert relation_in_scope(snapshot, proposal, RelationGenerationScope(("c1",), True))
    assert not relation_in_scope(
        snapshot, proposal, RelationGenerationScope(("i1",), False)
    )


def test_new_endpoint_uses_fallback_even_with_later_existing_target():
    snapshot = snapshot_fixture()
    evidence = real_example()[-1].document_proposals[0].evidence
    created = NewDocumentProposal(
        "new:CHARACTER:새 인물", "CHARACTER", "새 인물", "설정", evidence
    )
    proposal = replace(
        relation_between(snapshot, "c1", "i1"),
        document_id=created.candidate_id,
        base_revision_no=None,
    )
    first = scoped_input(("c1",), True)
    second = scoped_input(("i1",), False)
    assert relation_in_scope(snapshot, proposal, first.relation_scope)
    assert not relation_in_scope(snapshot, proposal, second.relation_scope)
    result = RefreshRules().validate_candidate(
        snapshot,
        ModelCandidate(
            (), relation_proposals=(proposal,), new_document_proposals=(created,)
        ),
        first,
    )
    assert result.outcome == Outcome.PROPOSED


def test_targetless_call_has_explicit_empty_owner_scope():
    snapshot = replace(snapshot_fixture(), target_ids=())
    proposal = relation_between(snapshot, "m1", "m2")
    result = RefreshRules().validate_candidate(
        snapshot,
        ModelCandidate((), relation_proposals=(proposal,)),
        scoped_input((), True),
    )
    assert result.outcome == Outcome.PROPOSED


def test_aggregate_and_legacy_none_scope_accept_all_owners():
    snapshot = snapshot_fixture()
    first = relation_between(snapshot, "c1", "i1")
    second = relation_between(snapshot, "m1", "i1")
    result = RefreshRules().validate_candidate(
        snapshot,
        ModelCandidate((), relation_proposals=(first, second)),
        ModelInput("fixed", "v3", "v5", "fake", snapshot.target_ids),
    )
    assert result.outcome == Outcome.PROPOSED
    assert len(result.relation_proposals) == 2


@pytest.mark.parametrize(
    "scope",
    [
        RelationGenerationScope(("i1",), False),
        RelationGenerationScope(("c1",), False),
        RelationGenerationScope(("c1", "c1"), True),
        "invalid",
    ],
)
def test_invalid_scope_metadata_fails_closed(scope):
    snapshot = snapshot_fixture()
    result = RefreshRules().validate_candidate(
        snapshot,
        ModelCandidate(()),
        replace(scoped_input(("c1",), True), relation_scope=scope),
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_RELATION_SCOPE"


def test_nonowner_relation_discards_independent_valid_update():
    snapshot = snapshot_fixture()
    original = real_example()[-1]
    candidate = replace(original, document_proposals=(original.document_proposals[1],))
    result = RefreshRules().validate_candidate(
        snapshot, candidate, scoped_input(("i1",), False)
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_RELATION_SCOPE"
    assert result.document_proposals == result.relation_proposals == ()


def test_owner_description_conflict_still_fails():
    snapshot = snapshot_fixture()
    proposal = relation_between(snapshot, "c1", "i1")
    candidate = ModelCandidate(
        (), relation_proposals=(proposal, replace(proposal, description="다른 설명"))
    )
    result = RefreshRules().validate_candidate(
        snapshot, candidate, scoped_input(("c1",), True)
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "CONFLICTING_PROPOSALS"


def test_model_inputs_carry_owner_metadata_and_keep_first_creation_scope():
    snapshot = snapshot_fixture()
    rules = RefreshRules()
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert [value.relation_scope for value in inputs] == [
        RelationGenerationScope(("c1",), True),
        RelationGenerationScope(("i1",), False),
    ]
    assert [value.allow_new_documents for value in inputs] == [True, False]
    for value in inputs:
        context = json.loads(value.prompt)
        assert context["relation_scope"]["owner_target_ids"] == list(value.target_ids)
        assert (
            context["relation_scope"]["owns_fallback"]
            == value.relation_scope.owns_fallback
        )
        assert context["relation_scope"]["target_order"] == list(snapshot.target_ids)
