"""순서 역전·관계 제거·영구 삭제를 실제 트랜잭션 Gremlin에서 검증한다."""

from uuid import uuid4

import pytest

from app.adapters.projection_ready import ProjectionReadiness
from app.kafka.errors import InvalidEvent, RetryableProcessingError
from app.projection.models import (
    FileState,
    ProjectionChange,
    ProjectionRejected,
    RelationState,
)

pytestmark = [pytest.mark.integration, pytest.mark.gremlin]


def change(project, file, sequence, **kwargs):
    return ProjectionChange(
        event_id=str(uuid4()),
        project_id=project,
        snapshot_file_id=file.id,
        sequence=sequence,
        files=(file,),
        **kwargs,
    )


def deletion(project, file_id, sequence):
    return ProjectionChange(
        event_id=str(uuid4()),
        project_id=project,
        sequence=sequence,
        snapshot_file_id=file_id,
        deleted_file_ids=(file_id,),
    )


def test_owner_snapshot_relations_and_fences(graph):
    store, query, project = graph
    pid = project()
    low, high = sorted((str(uuid4()), str(uuid4())))
    a = FileState(id=low, title="원고", folder_code="MANUSCRIPT")
    b = FileState(id=high, title="인물", folder_code="CHARACTER")
    relation = RelationState(
        id=str(uuid4()), low_document_id=low, high_document_id=high, description="등장"
    )
    # owner 관계가 대상의 첫 스냅샷보다 먼저 오면 대상은 숨긴 placeholder로 둔다.
    store.apply(change(pid, a, 5, relations=(relation,)))
    assert query(f"g.V('file:{high}').values('trashed')") == [True]
    assert query(f"g.E('rel:{relation.id}').count()") == [1]
    # high 문서의 스냅샷은 관계를 싣지 않으며 owner의 edge를 지우지 않는다.
    # 문서가 다르면 sequence가 작아도 반영한다.
    store.apply(change(pid, b, 2))
    assert query(f"g.V('file:{high}').values('trashed')") == [False]
    assert query(f"g.E('rel:{relation.id}').count()") == [1]
    # 같은 문서의 이전 sequence는 버린다.
    store.apply(change(pid, a.model_copy(update={"title": "이전"}), 4))
    assert query(f"g.V('file:{low}').values('title')") == ["원고"]
    # owner의 새 스냅샷에서 관계가 빠지면 edge를 지운다.
    store.apply(change(pid, a.model_copy(update={"title": "최신"}), 6))
    assert query(f"g.V('file:{low}').values('title')") == ["최신"]
    assert query(f"g.E('rel:{relation.id}').count()") == [0]
    # 영구 삭제된 대상으로는 edge와 placeholder를 다시 만들지 않는다.
    store.apply(deletion(pid, high, 3))
    store.apply(change(pid, a, 7, relations=(relation,)))
    assert query(f"g.E('rel:{relation.id}').count()") == [0]
    assert query(f"g.V('file:{high}').count()") == [0]


def test_trash_restore_and_document_project_tombstones(graph):
    store, query, project = graph
    pid = project()
    low, high = sorted((str(uuid4()), str(uuid4())))
    file = FileState(id=low, title="원고", folder_code="MANUSCRIPT")
    relation = RelationState(
        id=str(uuid4()), low_document_id=low, high_document_id=high
    )
    store.apply(change(pid, file, 1, relations=(relation,)))
    # 휴지통 이동도 전체 스냅샷이다. 관계는 남고 노드만 숨긴다.
    trashed = file.model_copy(update={"trashed": True})
    store.apply(change(pid, trashed, 2, relations=(relation,)))
    assert query(f"g.V('file:{file.id}').values('trashed')") == [True]
    assert query(f"g.E('rel:{relation.id}').count()") == [1]
    store.apply(change(pid, file, 3, relations=(relation,)))
    assert query(f"g.V('file:{file.id}').values('trashed')") == [False]
    store.apply(
        ProjectionChange(
            event_id=str(uuid4()),
            project_id=pid,
            sequence=4,
            snapshot_file_id=file.id,
            deleted_file_ids=(file.id,),
        )
    )
    store.apply(change(pid, file, 5))
    assert query(f"g.V('file:{file.id}').count()") == [0]
    store.apply(
        ProjectionChange(
            event_id=str(uuid4()),
            project_id=pid,
            sequence=6,
            event_type="ProjectDeleted",
        )
    )
    another = FileState(id=str(uuid4()), title="삭제 뒤", folder_code="CHARACTER")
    store.apply(change(pid, another, 7))
    assert query(f"g.V().hasLabel('File').has('project_id','{pid}').count()") == [0]


def test_marker_project_scope_and_readiness(graph):
    store, query, project = graph
    pid, other = project(), project()
    file = FileState(id=str(uuid4()), title="원고", folder_code="MANUSCRIPT")
    event = change(pid, file, 1)
    store.apply(event)
    with pytest.raises(ProjectionRejected):
        store.apply(change(other, file, 2))
    with ProjectionReadiness(store.endpoint) as ready:
        ready.check(pid, (event.event_id,))
        with pytest.raises(RetryableProcessingError):
            ready.check(pid, (str(uuid4()),))
        store.apply(
            ProjectionChange(
                event_id=str(uuid4()),
                project_id=pid,
                sequence=3,
                event_type="ProjectDeleted",
            )
        )
        with pytest.raises(InvalidEvent):
            ready.check(pid, ())


@pytest.mark.parametrize("kind", ["deleted", "trashed"])
def test_snapshot_cannot_mutate_legacy_file_in_another_project(graph, kind):
    store, query, project = graph
    owner, stranger = project(), project()
    file = FileState(id=str(uuid4()), title="다른 프로젝트", folder_code="MANUSCRIPT")
    store.apply(
        ProjectionChange(event_id=str(uuid4()), project_id=owner, files=(file,))
    )
    mutation = (
        {"deleted_file_ids": (file.id,)}
        if kind == "deleted"
        else {"files": (file.model_copy(update={"trashed": True}),)}
    )
    attempted = ProjectionChange(
        event_id=str(uuid4()),
        project_id=stranger,
        sequence=10,
        snapshot_file_id=file.id,
        **mutation,
    )
    with pytest.raises(ProjectionRejected):
        store.apply(attempted)
    assert query(f"g.V('file:{file.id}').values('trashed')") == [False]
    assert query(f"g.V('{attempted.inbox_id}').count()") == [0]
