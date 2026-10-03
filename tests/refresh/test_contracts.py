from dataclasses import FrozenInstanceError, replace

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    ArtifactRef,
    DocumentProposal,
    DocumentSnapshot,
    Evidence,
    Failure,
    InputSnapshot,
    JobKey,
    RefreshRequest,
    SelectionSettings,
)


def test_revision_and_original_positions_survive_snapshot_construction():
    job = JobKey("project", "request", "fingerprint")
    source = DocumentSnapshot("project", "draft", 4, "안녕\n안녕")
    snapshot = InputSnapshot(job, (source,))
    evidence = Evidence("draft", 4, 3, 5, "안녕")
    proposal = DocumentProposal("setting", 9, "body_text", "제안", (evidence,))
    assert (
        snapshot.documents[0].body_text[evidence.start : evidence.end] == evidence.quote
    )
    assert proposal.base_revision_no == 9
    assert proposal.evidence[0].revision_no == source.revision_no
    with pytest.raises(FrozenInstanceError):
        source.body_text = "changed"


def test_request_identity_includes_selection_and_input_location():
    request = RefreshRequest(JobKey("p", "r", "f"), ArtifactRef("b", "k"), ("d",))
    assert request != replace(request, settings=SelectionSettings(max_depth=2))
    assert request != replace(request, input_ref=ArtifactRef("b", "new"))


def test_expected_failure_preserves_retry_classification():
    failure = Failure("CONTENT_TIMEOUT", "try later", retryable=True)
    assert RefreshFailure(failure).failure is failure
