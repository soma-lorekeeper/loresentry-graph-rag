from dataclasses import replace

import pytest

from app.refresh.models import ModelCandidate, ModelInput, Outcome, Relation
from app.refresh.rules import RefreshRules
from tests.fakes.real_rules import real_example
from tests.refresh.test_context import snapshot_fixture


def validate(candidate, snapshot=None):
    return RefreshRules().validate_candidate(
        snapshot or snapshot_fixture(),
        candidate,
        ModelInput("fixed", "v1", "v1", "fake"),
    )


@pytest.mark.parametrize(
    "documents,relations,outcome",
    [
        (True, True, Outcome.PROPOSED),
        (True, False, Outcome.PROPOSED),
        (False, True, Outcome.PROPOSED),
        (False, False, Outcome.NO_CHANGE),
    ],
)
def test_result_modes(documents, relations, outcome):
    candidate = real_example()[-1]
    result = validate(
        replace(
            candidate,
            document_proposals=candidate.document_proposals if documents else (),
            relation_proposals=candidate.relation_proposals if relations else (),
        )
    )
    assert result.outcome == outcome


def test_reverse_duplicate_and_existing_edges():
    candidate = replace(real_example()[-1], document_proposals=())
    p = candidate.relation_proposals[0]
    reverse = replace(
        p,
        document_id=p.target_document_id,
        target_document_id=p.document_id,
        base_revision_no=p.target_base_revision_no,
        target_base_revision_no=p.base_revision_no,
        relation_key=p.reverse_relation_key,
        reverse_relation_key=p.relation_key,
    )
    assert (
        len(
            validate(
                replace(candidate, relation_proposals=(p, reverse, p))
            ).relation_proposals
        )
        == 1
    )
    snapshot = snapshot_fixture()
    snapshot = replace(
        snapshot,
        related=replace(
            snapshot.related,
            relations=snapshot.related.relations
            + (Relation("project", "i1", "c1", "related_character"),),
        ),
    )
    assert validate(candidate, snapshot).outcome == Outcome.NO_CHANGE


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"target_document_id": "c1"}, "INVALID_RELATION_ENDPOINT"),
        ({"document_id": "unknown"}, "INVALID_RELATION_ENDPOINT"),
        ({"relation_key": "friend"}, "INVALID_RELATION_KEY"),
        ({"relation_key": "related_character"}, "INVALID_RELATION_KEY"),
        ({"reverse_relation_key": "related_item"}, "INVALID_RELATION_KEY"),
        ({"base_revision_no": 8}, "REVISION_MISMATCH"),
        ({"target_base_revision_no": 8}, "REVISION_MISMATCH"),
        ({"operation": "DELETE"}, "INVALID_RELATION_KEY"),
        ({"description": " "}, "INVALID_RELATION_DESCRIPTION"),
        ({"evidence": ()}, "INVALID_EVIDENCE"),
    ],
)
def test_invalid_relation_discards_valid_document_proposals(changes, code):
    candidate = real_example()[-1]
    result = validate(
        replace(
            candidate,
            relation_proposals=(replace(candidate.relation_proposals[0], **changes),),
        )
    )
    assert result.outcome == Outcome.FAILED
    assert result.document_proposals == result.relation_proposals == ()
    assert result.failure.code == code


def test_invalid_document_discards_valid_relation():
    candidate = real_example()[-1]
    result = validate(
        replace(
            candidate,
            document_proposals=(
                replace(candidate.document_proposals[0], base_revision_no=99),
            ),
        )
    )
    assert result.outcome == Outcome.FAILED
    assert result.document_proposals == result.relation_proposals == ()


def test_conflicting_relation_descriptions_fail():
    candidate = real_example()[-1]
    relation = candidate.relation_proposals[0]
    result = validate(
        replace(
            candidate,
            relation_proposals=(relation, replace(relation, description="다른 관계")),
        )
    )
    assert result.failure.code == "CONFLICTING_PROPOSALS"


def test_invalid_response_type_fails_explicitly():
    assert validate("invalid").failure.code == "INVALID_MODEL_RESPONSE"
    assert (
        validate(ModelCandidate((), relation_proposals=("invalid",))).outcome
        == Outcome.FAILED
    )
