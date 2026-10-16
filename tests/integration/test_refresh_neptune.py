"""투영 저장→실제 원홉 조회→기존 Content 포트→refresh 스냅샷까지 연결한다."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.adapters.content_documents import ContentDocumentSource
from app.adapters.neptune_refresh import neptune_refresh_service
from app.adapters.neptune_relations import (
    NeptuneRelatedDocumentSource,
    build_one_hop_query,
)
from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.projection.models import FileState, ProjectionChange, RelationState
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    ArtifactRef,
    DocumentSnapshot,
    DocumentState,
    Failure,
    InputSnapshot,
    JobKey,
    ModelCandidate,
    ModelSettings,
    Outcome,
    Readiness,
    RefreshRequest,
)
from evaluation.fakes import (
    Calls,
    FakeDocuments,
    FakeInbox,
    FakeJsonArtifacts,
    FakeModel,
)
from tests.fakes.content import content_payload

pytestmark = [pytest.mark.integration, pytest.mark.gremlin]


def event(project_id, **changes):
    return ProjectionChange(event_id=str(uuid4()), project_id=project_id, **changes)


def request(project_id, *seeds):
    return RefreshRequest(
        JobKey(project_id, str(uuid4()), "immutable-input"),
        ArtifactRef("inputs", "refresh.json"),
        tuple(seeds),
        model_settings=ModelSettings(
            "fake", "refresh-prompt-v7", "refresh-candidate-v3"
        ),
    )


def file_state(identifier=None, folder="CHARACTER", **kwargs):
    return FileState(
        id=identifier or str(uuid4()), title="문서", folder_code=folder, **kwargs
    )


def relation(left, right, description="연결"):
    low, high = sorted((left.id, right.id))
    return RelationState(
        id=str(uuid4()),
        low_document_id=low,
        high_document_id=high,
        description=description,
    )


def test_both_edge_directions_one_hop_and_category_filter(graph):
    store, query, project = graph
    p = project()
    left, middle, right = sorted(str(uuid4()) for _ in range(3))
    left = file_state(left, "CHARACTER")
    middle = file_state(middle, "MANUSCRIPT")
    right = file_state(right, "LOCATION")
    second_hop = file_state(folder="ITEM")
    store.apply(
        event(
            p,
            files=(left, middle, right, second_hop),
            relations=(
                relation(left, middle),
                relation(middle, right),
                relation(right, second_hop),
            ),
        )
    )
    req = request(p, middle.id)
    with NeptuneRelatedDocumentSource(store.endpoint) as source:
        found = source.find_related(req)
        assert set(found.document_ids) == {left.id, right.id}
        assert {rel.relation_key for rel in found.relations} == {
            "related_character",
            "related_place",
        }
        assert all(rel.document_id == middle.id for rel in found.relations)
        filtered = source.find_related(
            replace(
                req,
                settings=replace(req.settings, relation_keys=("related_character",)),
            )
        )
        assert filtered.document_ids == (left.id,)
    # Neptune과 같은 ANTLR 엔진에서도 읽기 traversal을 실행한다.
    value = query(build_one_hop_query(p, req.seed_ids, (), 60), language="gremlin-lang")
    assert len(value[0]["relations"]) == 2


def test_trash_foreign_projects_wrong_labels_and_edges_are_excluded(graph):
    store, query, project = graph
    p, other = project(), project()
    seed = file_state(folder="MANUSCRIPT")
    active, trashed, foreign, unrelated = (
        file_state(),
        file_state(trashed=True),
        file_state(),
        file_state(),
    )
    store.apply(
        event(
            p,
            files=(seed, active, trashed, unrelated),
            relations=(relation(seed, active), relation(seed, trashed)),
        )
    )
    store.apply(event(other, files=(foreign,)))
    # 저장 어댑터를 우회한 잘못된 edge도 조회가 프로젝트 경계를 넘지 않아야 한다.
    query(
        f"g.V('file:{seed.id}').addE('REFERENCES').to(__.V('file:{foreign.id}')).property('project_id','{p}').property('description','foreign')"
    )
    query(
        f"g.V('file:{seed.id}').addE('OTHER').to(__.V('file:{unrelated.id}')).property('project_id','{p}')"
    )
    with NeptuneRelatedDocumentSource(store.endpoint) as source:
        assert source.find_related(request(p, seed.id)).document_ids == (active.id,)
        assert (
            source.find_related(request(p, trashed.id)).readiness == Readiness.WAITING
        )
        assert (
            source.find_related(request(p, foreign.id)).readiness == Readiness.WAITING
        )


def test_refresh_fetches_only_one_hop_content_body_and_preserves_input(
    graph, content_server
):
    store, query, project = graph
    p = project()
    seed, neighbor, farther = (
        file_state(folder="MANUSCRIPT"),
        file_state(),
        file_state(folder="ITEM"),
    )
    store.apply(
        event(
            p,
            files=(seed, neighbor, farther),
            relations=(relation(seed, neighbor), relation(neighbor, farther)),
        )
    )
    req = request(p, seed.id)
    original = DocumentSnapshot(p, seed.id, 3, "입력에서 보존한 원고 본문")
    content = DocumentSnapshot(
        p,
        neighbor.id,
        7,
        "Content에서 읽은 현재 본문",
        folder_code="CHARACTER",
        title="기존 인물",
        properties=(("aliases", "별칭  "),),
    )
    state, content_endpoint = content_server
    user_id = str(uuid4())
    state["responses"][f"/projects/{p}"] = (200, {"id": p, "trashed_at": None})
    state["responses"][f"/files/{neighbor.id}/content"] = (
        200,
        content_payload(p, neighbor.id, text=content.body_text),
    )
    calls = Calls()
    artifacts = FakeJsonArtifacts(
        {req.input_ref: InputSnapshot(req.job, (original,))}, calls
    )
    before = query(f"g.V().hasLabel('File').has('project_id','{p}').count()")
    with neptune_refresh_service(
        endpoint=store.endpoint,
        artifacts=artifacts,
        user_id=user_id,
        content_base_url=content_endpoint,
        model=FakeModel(ModelCandidate(()), calls),
    ) as service:
        assert isinstance(service.relations, NeptuneRelatedDocumentSource)
        assert isinstance(service.documents, ContentDocumentSource)
        assert isinstance(service.inbox, NeptuneRefreshInbox)
        completion = service.run(req)
        assert completion.outcome == Outcome.NO_CHANGE
        assert service.inbox.find(req) == completion
        assert service.run(req) == completion
    assert [path for path, _ in state["requests"]] == [
        f"/projects/{p}",
        f"/files/{neighbor.id}/content",
    ]
    assert all(headers["X-User-Id"] == user_id for _, headers in state["requests"])
    assert service.documents._client.is_closed
    snapshot = artifacts.read_context(req)
    assert {doc.document_id for doc in snapshot.documents} == {seed.id, neighbor.id}
    assert (
        next(doc for doc in snapshot.documents if doc.document_id == seed.id)
        == original
    )
    assert (
        next(doc for doc in snapshot.documents if doc.document_id == neighbor.id)
        == content
    )
    assert snapshot.target_ids == (neighbor.id,)
    assert query(f"g.V().hasLabel('File').has('project_id','{p}').count()") == before
    with NeptuneRefreshInbox(store.endpoint) as inbox:
        assert any(entry.completion == completion for entry in inbox.pending())


def test_missing_projection_blocks_content_and_model_until_seed_is_available(graph):
    store, _, project = graph
    p = project()
    seed = file_state(folder="MANUSCRIPT")
    req = request(p, seed.id)
    calls = Calls()
    artifacts = FakeJsonArtifacts(
        {
            req.input_ref: InputSnapshot(
                req.job, (DocumentSnapshot(p, seed.id, 1, "본문"),)
            )
        },
        calls,
    )
    with neptune_refresh_service(
        endpoint=store.endpoint,
        artifacts=artifacts,
        documents=FakeDocuments(calls=calls),
        model=FakeModel(ModelCandidate(()), calls),
        inbox=FakeInbox(calls=calls),
    ) as service:
        with pytest.raises(RefreshFailure) as caught:
            service.run(req)
        assert caught.value.failure.code == "GRAPH_NOT_READY"
        assert "documents.fetch_documents" not in calls.names()
        assert "model.generate" not in calls.names()
        store.apply(event(p, files=(seed,)))
        assert service.run(req).outcome == Outcome.NO_CHANGE


def test_inactive_input_seeds_do_not_wait_for_projection(graph):
    store, _, project = graph
    p = project()
    req = request(p, str(uuid4()))
    calls = Calls()
    original = DocumentSnapshot(
        p, req.seed_ids[0], 1, None, state=DocumentState.TRASHED
    )
    artifacts = FakeJsonArtifacts(
        {req.input_ref: InputSnapshot(req.job, (original,))}, calls
    )
    with neptune_refresh_service(
        endpoint=store.endpoint,
        artifacts=artifacts,
        documents=FakeDocuments(calls=calls),
        model=FakeModel(ModelCandidate(()), calls),
        inbox=FakeInbox(calls=calls),
    ) as service:
        assert service.run(req).outcome == Outcome.NO_CHANGE
    assert "documents.fetch_documents" not in calls.names()
    assert "model.generate" not in calls.names()


def test_retry_uses_frozen_context_even_if_graph_and_content_change(graph):
    store, _, project = graph
    p = project()
    seed, neighbor = file_state(folder="MANUSCRIPT"), file_state()
    store.apply(event(p, files=(seed, neighbor), relations=(relation(seed, neighbor),)))
    req = request(p, seed.id)
    calls = Calls()
    original = DocumentSnapshot(p, seed.id, 2, "원본 원고")
    target = DocumentSnapshot(p, neighbor.id, 4, "원본 설정", folder_code="CHARACTER")
    artifacts = FakeJsonArtifacts(
        {req.input_ref: InputSnapshot(req.job, (original,))}, calls
    )
    documents = FakeDocuments((target,), calls)
    model = FakeModel(ModelCandidate(()), calls)
    model.responses.append(RefreshFailure(Failure("MODEL_TEMPORARY", "retry", True)))
    with neptune_refresh_service(
        endpoint=store.endpoint,
        artifacts=artifacts,
        documents=documents,
        model=model,
        inbox=FakeInbox(calls=calls),
    ) as service:
        with pytest.raises(RefreshFailure):
            service.run(req)
        frozen = artifacts.read_context(req)
        store.apply(event(p, deleted_file_ids=(seed.id, neighbor.id)))
        documents.documents.clear()
        assert service.run(req).outcome == Outcome.NO_CHANGE
    assert artifacts.read_context(req) == frozen
    assert calls.names().count("documents.fetch_documents") == 1
