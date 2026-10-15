"""실제 TinkerTransactionGraph에서 검증한다. Neptune 자체를 대체하지 않는다."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from app.adapters.neptune_graph import NeptuneProjectionStore
from app.adapters.projection_gremlin import build_projection_query
from app.projection.fake import FakeConsumer, FakeRecord
from app.projection.models import (
    ApplyResult,
    FileState,
    ProjectionChange,
    ProjectionRejected,
    ProjectionUnavailable,
    RelationState,
)
from app.projection.service import GraphProjectionHandler

pytestmark = [pytest.mark.integration, pytest.mark.gremlin]


def event(project_id, **kwargs):
    return ProjectionChange(event_id=str(uuid4()), project_id=project_id, **kwargs)


def pair():
    low, high = sorted((str(uuid4()), str(uuid4())))
    files = (
        FileState(id=low, title="원고", folder_code="MANUSCRIPT"),
        FileState(id=high, title="캐릭터", folder_code="CHARACTER"),
    )
    relation = RelationState(
        id=str(uuid4()),
        low_document_id=low,
        high_document_id=high,
        description="첫 등장",
    )
    return files, relation


def test_create_modify_delete_and_late_duplicate_does_not_resurrect(graph):
    store, query, project = graph
    project_id = project()
    files, relation = pair()
    original = event(project_id, files=files, relations=(relation,))
    assert store.apply(original) == ApplyResult.APPLIED
    assert store.apply(original) == ApplyResult.DUPLICATE
    assert query(
        f"g.V().hasLabel('File').has('project_id','{project_id}').count()"
    ) == [2]
    assert query(f"g.E('rel:{relation.id}').count()") == [1]
    assert query(f"g.E('rel:{relation.id}').outV().id()") == [f"file:{files[0].id}"]
    description = "바뀐 설명"
    store.apply(
        event(
            project_id,
            relations=(relation.model_copy(update={"description": description}),),
        )
    )
    assert query(f"g.E('rel:{relation.id}').values('description')") == [description]
    store.apply(event(project_id, deleted_relation_ids=(relation.id,)))
    assert store.apply(original) == ApplyResult.DUPLICATE
    assert query(f"g.E('rel:{relation.id}').count()") == [0]


def test_missing_endpoint_rolls_back_inbox_and_prior_file_write_then_can_retry(graph):
    store, query, project = graph
    project_id = project()
    files, relation = pair()
    original = event(project_id, files=(files[0],), relations=(relation,))
    with pytest.raises(ProjectionRejected, match="MISSING_FILE"):
        store.apply(original)
    assert query(f"g.V('file:{files[0].id}').count()") == [0]
    assert query(f"g.V('{original.inbox_id}').count()") == [0]
    store.apply(event(project_id, files=(files[1],)))
    assert store.apply(original) == ApplyResult.APPLIED
    assert query(f"g.E('rel:{relation.id}').count()") == [1]


def test_payload_id_reuse_and_duplicate_pair_are_rejected_atomically(graph):
    store, query, project = graph
    project_id = project()
    files, relation = pair()
    original = event(project_id, files=files, relations=(relation,))
    store.apply(original)
    changed = original.model_copy(
        update={"files": (files[0].model_copy(update={"title": "다른 내용"}), files[1])}
    )
    with pytest.raises(ProjectionRejected, match="INBOX_CONFLICT"):
        store.apply(changed)
    assert query(f"g.V('file:{files[0].id}').values('title')") == [files[0].title]
    second = event(
        project_id, relations=(relation.model_copy(update={"id": str(uuid4())}),)
    )
    with pytest.raises(ProjectionRejected, match="PAIR_CONFLICT"):
        store.apply(second)
    assert query(f"g.V('{second.inbox_id}').count()") == [0]
    assert query(f"g.E().has('project_id','{project_id}').count()") == [1]


def test_foreign_project_node_cannot_be_overwritten_deleted_or_linked(graph):
    store, query, project = graph
    first, other = project(), project()
    files, relation = pair()
    store.apply(event(first, files=(files[0],)))
    store.apply(event(other, files=(files[1],)))
    for invalid in (
        event(first, files=(files[1],)),
        event(first, deleted_file_ids=(files[1].id,)),
        event(first, relations=(relation,)),
    ):
        with pytest.raises(ProjectionRejected, match="PROJECT_MISMATCH"):
            store.apply(invalid)
        assert query(f"g.V('{invalid.inbox_id}').count()") == [0]
    assert query(f"g.V('file:{files[1].id}').values('project_id')") == [other]


def test_trash_restore_permanent_delete_and_project_delete_preserve_inbox(graph):
    store, query, project = graph
    project_id, other = project(), project()
    files, relation = pair()
    original = event(project_id, files=files, relations=(relation,))
    store.apply(original)
    other_file = FileState(id=str(uuid4()), title="다른 프로젝트", folder_code="ITEM")
    store.apply(event(other, files=(other_file,)))
    store.apply(
        event(project_id, files=(files[1].model_copy(update={"trashed": True}),))
    )
    assert query(f"g.E('rel:{relation.id}').count()") == [1]
    active_edges = (
        f"g.E('rel:{relation.id}')"
        ".filter(__.outV().has('trashed',false))"
        ".filter(__.inV().has('trashed',false)).count()"
    )
    assert query(active_edges) == [0]
    store.apply(event(project_id, files=(files[1],)))
    assert query(active_edges) == [1]
    assert query(f"g.V('file:{files[1].id}').values('trashed')") == [False]
    store.apply(event(project_id, deleted_file_ids=(files[1].id,)))
    assert query(f"g.E('rel:{relation.id}').count()") == [0]
    deletion = event(project_id, event_type="ProjectDeleted")
    assert store.apply(deletion) == ApplyResult.APPLIED
    assert query(
        f"g.V().hasLabel('File').has('project_id','{project_id}').count()"
    ) == [0]
    assert query(f"g.V('{original.inbox_id}','{deletion.inbox_id}').count()") == [2]
    assert store.apply(original) == ApplyResult.DUPLICATE
    assert query(f"g.V('file:{other_file.id}').count()") == [1]


def test_text_escaping_and_neptune_antlr_grammar(graph):
    store, query, project = graph
    project_id = project()
    title = "제목 ' \\\n\t\r\b\f \" ${g.V().drop()} 한글 😀"
    file = FileState(id=str(uuid4()), title=title, folder_code="EVENT")
    change = event(project_id, files=(file,))
    # Neptune과 같은 ANTLR 언어 엔진에서도 파싱·실행한다.
    assert query(
        build_projection_query(change, "2026-10-15T00:00:00Z"), language="gremlin-lang"
    ) == ["APPLIED"]
    assert store.apply(change) == ApplyResult.DUPLICATE
    assert query(f"g.V('file:{file.id}').values('title')") == [title]


def test_concurrent_delivery_commits_once(graph):
    store, query, project = graph
    project_id = project()
    files, relation = pair()
    change = event(project_id, files=files, relations=(relation,))

    def apply_with_conflict_retry(_):
        for _attempt in range(10):
            try:
                return store.apply(change)
            except ProjectionUnavailable:
                continue
        pytest.fail("Concurrent transaction never settled")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(apply_with_conflict_retry, range(4)))
    assert results.count(ApplyResult.APPLIED) == 1
    assert results.count(ApplyResult.DUPLICATE) == 3
    assert query(f"g.V('{change.inbox_id}').count()") == [1]
    assert query(f"g.E('rel:{relation.id}').count()") == [1]


def test_commit_response_loss_is_duplicate_on_retry_and_does_not_advance_ack(graph):
    store, query, project = graph
    project_id = project()
    files, relation = pair()
    first = event(project_id, files=files, relations=(relation,))
    later = event(project_id, deleted_relation_ids=(relation.id,))
    lose_response = True

    with httpx.Client(timeout=20) as backend:

        def forward(request):
            nonlocal lose_response
            response = backend.send(request)
            if lose_response:
                lose_response = False
                response.close()
                raise httpx.ReadTimeout(
                    "Injected lost commit response", request=request
                )
            return response

        with httpx.Client(transport=httpx.MockTransport(forward)) as client:
            proxy = NeptuneProjectionStore(store.endpoint, client=client)
            consumer = FakeConsumer(
                GraphProjectionHandler(proxy),
                (
                    FakeRecord(project_id, first, offset=0),
                    FakeRecord(project_id, later, offset=1),
                ),
            )
            consumer.run()
            assert consumer.committed_offsets == {}
            assert query(f"g.V('{first.inbox_id}').count()") == [1]
            assert query(f"g.E('rel:{relation.id}').count()") == [1]
            consumer.resume(0)
            consumer.run()
    assert consumer.committed_offsets == {0: 2}
    assert [result for _, result in consumer.results] == [
        ApplyResult.DUPLICATE,
        ApplyResult.APPLIED,
    ]
    assert query(f"g.E('rel:{relation.id}').count()") == [0]
