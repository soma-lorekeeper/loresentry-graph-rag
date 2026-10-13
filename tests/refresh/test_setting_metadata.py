import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.adapters.refresh_artifacts import S3ArtifactSettings, S3RefreshArtifacts
from app.adapters.refresh_schema import (
    ContextObject,
    InputObject,
    context_bytes,
    fingerprint,
    input_bytes,
    json_bytes,
)
from app.refresh.errors import RequestConflict
from app.refresh.models import (
    DocumentSnapshot,
    DocumentState,
    InputSnapshot,
    JobKey,
    ModelCandidate,
    ModelInput,
    NewDocumentProposal,
    Outcome,
)
from app.refresh.rules import RefreshRules
from tests.fakes.real_rules import real_example
from tests.fakes.refresh import FakeArtifacts, FakeDocuments
from tests.refresh.test_context import snapshot_fixture


def validate_named_setting(snapshot, name, folder_code="CHARACTER"):
    proposal = NewDocumentProposal(
        f"new:{folder_code}:{name}",
        folder_code,
        name,
        "원고에서 확인한 설정",
        real_example()[-1].document_proposals[0].evidence,
    )
    return RefreshRules().validate_candidate(
        snapshot,
        ModelCandidate((), new_document_proposals=(proposal,)),
        ModelInput("fixed", "v2", "v3", "fake", snapshot.target_ids),
    )


def with_setting_metadata(**changes):
    snapshot = snapshot_fixture()
    return replace(
        snapshot,
        documents=tuple(
            replace(document, **changes) if document.document_id == "c1" else document
            for document in snapshot.documents
        ),
    )


@pytest.mark.parametrize(
    "changes,name",
    [
        ({"title": "유나", "properties": ()}, "유나"),
        ({"properties": (("name", "유나"),)}, "유나"),
        ({"properties": (("title", "유나"),)}, "유나"),
        ({"title": "유나", "properties": (("alias", "윤아"),)}, "윤아"),
        ({"title": "ＦＯＯ  Bar"}, "foo bar"),
        ({"properties": (("alias", "ＦＯＯ  Bar"),)}, "foo bar"),
    ],
)
def test_existing_title_name_and_explicit_alias_reject_duplicate(changes, name):
    snapshot = with_setting_metadata(**changes)
    result = validate_named_setting(snapshot, name)
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "DUPLICATE_SETTING"
    assert result.new_document_proposals == ()
    setting = next(d for d in snapshot.documents if d.document_id == "c1")
    assert setting.title == changes.get("title")
    if "properties" in changes:
        assert setting.properties == changes["properties"]


@pytest.mark.parametrize(
    "changes",
    [
        {"state": DocumentState.TRASHED},
        {"state": DocumentState.DELETED},
        {"project_id": "other-project"},
        {"folder_code": "MANUSCRIPT"},
    ],
)
def test_names_outside_active_project_settings_do_not_block_creation(changes):
    snapshot = with_setting_metadata(title="유나", **changes)
    assert validate_named_setting(snapshot, "유나").outcome == Outcome.PROPOSED


def test_duplicate_identity_keeps_setting_classification():
    snapshot = with_setting_metadata(title="유나")
    assert (
        validate_named_setting(snapshot, "유나", "LOCATION").outcome == Outcome.PROPOSED
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "유나", "body_text": "윤아라는 인물을 만났다."},
        {"properties": (("alias", "윤아, 유나"),)},
        {"properties": (("aliases", "윤아"),)},
    ],
)
def test_body_mentions_and_uncontracted_alias_formats_are_not_identity(changes):
    snapshot = with_setting_metadata(**changes)
    assert validate_named_setting(snapshot, "윤아").outcome == Outcome.PROPOSED


def test_input_title_and_alias_roundtrip_preserves_original_metadata():
    document = DocumentSnapshot(
        "project",
        "setting",
        1,
        "원문\n😀",
        properties=(("alias", " ＦＯＯ  Bar "),),
        folder_code="CHARACTER",
        title=" 유나 ",
    )
    source = InputSnapshot(JobKey("project", "request", "input"), (document,))
    body = input_bytes(source)
    restored = InputObject.model_validate_json(body)
    assert restored.documents == source.documents
    assert json.loads(body)["documents"][0]["title"] == " 유나 "


def test_missing_title_keeps_legacy_input_bytes_and_fingerprint():
    legacy = {
        "schema_version": "refresh-input-v1",
        "project_id": "project",
        "request_id": "request",
        "documents": [
            {
                "project_id": "project",
                "document_id": "m1",
                "revision_no": 1,
                "body_text": None,
                "properties": [],
                "relations": [],
                "state": "DELETED",
                "folder_code": "MANUSCRIPT",
            }
        ],
    }
    body = json_bytes(legacy)
    restored = InputObject.model_validate_json(body)
    source = InputSnapshot(
        JobKey("project", "request", fingerprint(body)), restored.documents
    )
    assert restored.documents[0].title is None
    assert input_bytes(source) == body
    assert fingerprint(input_bytes(source)) == source.job.input_fingerprint
    assert json.loads(input_bytes(source))["documents"][0]["body_text"] is None


def test_legacy_context_replay_preserves_canonical_bytes_and_immutability():
    snapshot = snapshot_fixture()
    legacy = ContextObject(
        schema_version="refresh-context-v1", snapshot=snapshot
    ).model_dump(mode="json")
    for document in legacy["snapshot"]["documents"]:
        document.pop("title", None)
    legacy["snapshot"]["request"]["settings"].pop("separate_discovery", None)
    legacy["snapshot"]["request"]["settings"].pop("chunk_overlap_chars", None)
    body = json_bytes(legacy)
    restored = ContextObject.model_validate_json(body).snapshot
    assert restored == snapshot
    assert context_bytes(restored) == body
    store = S3RefreshArtifacts(
        Mock(), S3ArtifactSettings("artifact-bucket", "input-bucket")
    )
    store._read = Mock(return_value=body)
    store._check_request = Mock(return_value=True)
    assert store.save_context(restored) == store._ref(snapshot.request, "context")
    changed = replace(
        restored,
        documents=(replace(restored.documents[0], title="새 제목"),)
        + restored.documents[1:],
    )
    with pytest.raises(RequestConflict, match="immutable"):
        store.save_context(changed)


def test_separate_discovery_policy_is_saved_and_cannot_change_on_replay():
    snapshot = snapshot_fixture()
    updated = replace(
        snapshot,
        request=replace(
            snapshot.request,
            settings=replace(snapshot.request.settings, separate_discovery=True),
        ),
    )
    body = context_bytes(updated)
    assert (
        json.loads(body)["snapshot"]["request"]["settings"]["separate_discovery"]
        is True
    )
    assert ContextObject.model_validate_json(body).snapshot == updated
    store = S3RefreshArtifacts(
        Mock(), S3ArtifactSettings("artifact-bucket", "input-bucket")
    )
    store._read = Mock(return_value=context_bytes(snapshot))
    store._check_request = Mock(return_value=True)
    with pytest.raises(RequestConflict, match="immutable"):
        store.save_context(updated)


def test_stored_context_and_fakes_preserve_title_and_alias():
    snapshot = with_setting_metadata(title="유나", properties=(("alias", " 윤아 "),))
    restored = ContextObject.model_validate_json(context_bytes(snapshot)).snapshot
    assert restored == snapshot
    source = InputSnapshot(snapshot.request.job, snapshot.documents)
    store = FakeArtifacts({snapshot.request.input_ref: source})
    store.save_context(snapshot)
    assert store.read_context(snapshot.request) == snapshot
    assert store.read_input(snapshot.request) == source
    batch = FakeDocuments(snapshot.documents).fetch_documents("project", ("c1",))
    setting = next(d for d in snapshot.documents if d.document_id == "c1")
    assert batch.documents == (setting,)
