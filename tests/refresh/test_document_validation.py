from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentState
from app.refresh.validation import validate_document_proposals
from tests.fakes.real_rules import real_example
from tests.refresh.test_context import snapshot_fixture


def test_valid_multi_document_proposals_and_dedup():
    snapshot = snapshot_fixture()
    candidate = real_example()[-1]
    assert (
        validate_document_proposals(snapshot, candidate.document_proposals * 2)
        == candidate.document_proposals
    )
    p = candidate.document_proposals[0]
    current = next(
        d for d in snapshot.documents if d.document_id == p.target_document_id
    )
    assert (
        validate_document_proposals(snapshot, (replace(p, value=current.body_text),))
        == ()
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"target_document_id": "missing"}, "INVALID_TARGET"),
        ({"target_document_id": "m1"}, "INVALID_TARGET"),
        ({"base_revision_no": 8}, "REVISION_MISMATCH"),
        ({"base_revision_no": True}, "REVISION_MISMATCH"),
        ({"field": "body"}, "INVALID_FIELD"),
        ({"value": {}}, "INVALID_FIELD"),
        ({"value": "가" * 5001}, "INVALID_FIELD"),
        ({"evidence": ()}, "INVALID_EVIDENCE"),
    ],
)
def test_invalid_document_candidate(changes, code):
    snapshot = snapshot_fixture()
    proposal = replace(real_example()[-1].document_proposals[0], **changes)
    with pytest.raises(RefreshFailure) as caught:
        validate_document_proposals(snapshot, (proposal,))
    assert caught.value.failure.code == code


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": "missing"},
        {"revision_no": 99},
        {"start": -1},
        {"start": True},
        {"end": 99999},
        {"quote": "invented"},
    ],
)
def test_invalid_evidence(changes):
    proposal = real_example()[-1].document_proposals[0]
    proposal = replace(proposal, evidence=(replace(proposal.evidence[0], **changes),))
    with pytest.raises(RefreshFailure) as caught:
        validate_document_proposals(snapshot_fixture(), (proposal,))
    assert caught.value.failure.code == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": "other"},
        {"state": DocumentState.TRASHED},
        {"state": DocumentState.DELETED},
    ],
)
def test_target_scope_state(changes):
    snapshot = snapshot_fixture()
    snapshot = replace(
        snapshot,
        documents=tuple(
            replace(d, **changes) if d.document_id == "c1" else d
            for d in snapshot.documents
        ),
    )
    with pytest.raises(RefreshFailure):
        validate_document_proposals(snapshot, real_example()[-1].document_proposals)


def test_conflicting_values_fail_including_conflict_with_noop():
    snapshot = snapshot_fixture()
    proposal = real_example()[-1].document_proposals[0]
    for text in [
        "다른 변경",
        next(d.body_text for d in snapshot.documents if d.document_id == "c1"),
    ]:
        with pytest.raises(RefreshFailure) as caught:
            validate_document_proposals(
                snapshot, (proposal, replace(proposal, value=text))
            )
        assert caught.value.failure.code == "CONFLICTING_PROPOSALS"
