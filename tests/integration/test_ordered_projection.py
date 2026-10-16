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


def test_latest_file_snapshot_and_relation_fences(graph):
    store, query, project = graph
    pid = project()
    low, high = sorted((str(uuid4()), str(uuid4())))
    a = FileState(id=low, title="원고", folder_code="MANUSCRIPT")
    b = FileState(id=high, title="인물", folder_code="CHARACTER")
    relation = RelationState(
        id=str(uuid4()), low_document_id=low, high_document_id=high, description="등장"
    )
    store.apply(change(pid, a, 1, relations=(relation,)))
    assert query(f"g.V('file:{high}').values('trashed')") == [True]
    store.apply(change(pid, b, 2, relations=(relation,)))
    assert query(f"g.E('rel:{relation.id}').count()") == [1]
    store.apply(change(pid, a.model_copy(update={"title": "최신"}), 4))
    assert query(f"g.E('rel:{relation.id}').count()") == [0]
    store.apply(change(pid, b, 3, relations=(relation,)))
    assert query(f"g.E('rel:{relation.id}').count()") == [0]
    store.apply(change(pid, a, 1, relations=(relation,)))
    assert query(f"g.V('file:{low}').values('title')") == ["최신"]
    assert query(f"g.E('rel:{relation.id}').count()") == [0]


def test_trash_restore_and_document_project_tombstones(graph):
    store, query, project = graph
    pid = project()
    file = FileState(id=str(uuid4()), title="원고", folder_code="MANUSCRIPT")
    store.apply(change(pid, file, 1))
    store.apply(
        ProjectionChange(
            event_id=str(uuid4()),
            project_id=pid,
            sequence=2,
            snapshot_file_id=file.id,
            trashed_file_ids=(file.id,),
        )
    )
    assert query(f"g.V('file:{file.id}').values('trashed')") == [True]
    store.apply(change(pid, file, 3))
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


@pytest.mark.parametrize("field", ["deleted_file_ids", "trashed_file_ids"])
def test_inactive_snapshot_cannot_mutate_legacy_file_in_another_project(graph, field):
    store, query, project = graph
    owner, stranger = project(), project()
    file = FileState(id=str(uuid4()), title="다른 프로젝트", folder_code="MANUSCRIPT")
    store.apply(
        ProjectionChange(event_id=str(uuid4()), project_id=owner, files=(file,))
    )
    attempted = ProjectionChange(
        event_id=str(uuid4()),
        project_id=stranger,
        sequence=10,
        snapshot_file_id=file.id,
        **{field: (file.id,)},
    )
    with pytest.raises(ProjectionRejected):
        store.apply(attempted)
    assert query(f"g.V('file:{file.id}').values('trashed')") == [False]
    assert query(f"g.V('{attempted.inbox_id}').count()") == [0]
