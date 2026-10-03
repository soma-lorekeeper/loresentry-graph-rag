from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import DocumentState, Failure, Readiness
from tests.fakes.fixtures import example
from tests.fakes.refresh import Calls, FakeArtifacts, FakeDocuments, FakeRelations


def test_artifact_round_trip_is_immutable_and_separates_input_and_execution():
    request, source, _, snapshot, _, result = example()
    inputs = {request.input_ref: source}
    store = FakeArtifacts(inputs)
    inputs.clear()
    assert store.read_input(request) == source
    assert store.read_context(request) is None
    assert store.save_context(snapshot) == store.save_context(snapshot)
    assert store.read_context(request) == snapshot
    assert store.read_input(request) == source
    assert store.save_result(request, result) == store.save_result(request, result)
    assert store.read_result(request) == result
    with pytest.raises(RequestConflict):
        store.save_context(replace(snapshot, documents=()))
    with pytest.raises(RequestConflict):
        store.read_result(replace(request, target_ids=("different",)))
    with pytest.raises(RequestConflict):
        store.save_result(request, replace(result, prompt_version="different"))
    assert FakeArtifacts().read_result(request) is None


@pytest.mark.parametrize("code", ["INPUT_EXPIRED", "ACCESS_DENIED", "STORAGE_TIMEOUT"])
def test_artifact_failures_are_explicit_and_failed_save_has_no_effect(code):
    request, source, _, snapshot, _, _ = example()
    calls = Calls()
    store = FakeArtifacts({request.input_ref: source}, calls)
    calls.failures["artifacts.save_context"].append(RefreshFailure(Failure(code, code)))
    with pytest.raises(RefreshFailure) as failure:
        store.save_context(snapshot)
    assert failure.value.failure.code == code
    assert store.read_context(request) is None
    assert store.read_input(request) == source
    with pytest.raises(RefreshFailure, match="Input unavailable"):
        FakeArtifacts().read_input(request)


@pytest.mark.parametrize("readiness", list(Readiness))
def test_relation_fixture_preserves_duplicates_cycles_scope_and_readiness(readiness):
    request, _, related, _, _, _ = example()
    edge = related.relations[0]
    response = replace(
        related,
        readiness=readiness,
        document_ids=("setting-1", "setting-1", "draft-1"),
        relations=(
            edge,
            replace(
                edge,
                document_id=edge.target_document_id,
                target_document_id=edge.document_id,
                relation_key="related_manuscript",
            ),
            replace(edge, project_id="other"),
        ),
    )
    fake = FakeRelations(response)
    assert fake.find_related(request) == response
    assert fake.find_related(request) is not response


def test_document_fake_reports_missing_deleted_and_latest_fixture_revision():
    _, _, _, snapshot, _, _ = example()
    setting = snapshot.documents[1]
    documents = FakeDocuments((setting,))
    batch = documents.fetch_documents("project-1", ("setting-1", "missing"))
    assert batch.documents == (setting,)
    assert batch.missing_ids == ("missing",)
    documents.documents[("project-1", "setting-1")] = replace(
        setting, revision_no=8, state=DocumentState.DELETED
    )
    updated = documents.fetch_documents("project-1", ("setting-1",))
    assert updated.documents[0].state == DocumentState.DELETED
    assert batch.documents[0].revision_no == 7
    assert documents.fetch_documents("other", ("setting-1",)).missing_ids == (
        "setting-1",
    )


@pytest.mark.parametrize("code", ["CONTENT_DENIED", "CONTENT_TIMEOUT"])
def test_document_errors_and_trace_are_isolated(code):
    calls = Calls()
    calls.failures["documents.fetch_documents"].append(
        RefreshFailure(Failure(code, code))
    )
    fake = FakeDocuments(calls=calls)
    with pytest.raises(RefreshFailure):
        fake.fetch_documents("p", ("d",))
    assert calls.events == [("documents.fetch_documents", ("p", ("d",)))]
    assert FakeDocuments().calls.events == []
