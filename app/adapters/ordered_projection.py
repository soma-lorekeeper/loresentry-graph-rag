"""Content의 전체 문서 스냅샷을 sequence fence·삭제 tombstone과 원자적으로 반영한다."""

from app.adapters.projection_gremlin import (
    PROJECT_MISMATCH,
    _edge,
    _edge_scope,
    _file_scope,
    _guard,
    _properties,
    _vertex,
    literal,
)


def file_marker(identifier):
    return literal(f"projection:file:{identifier}")


def project_marker(identifier):
    return literal(f"projection:project:{identifier}")


def _state(identifier, project_id):
    return (
        f"__.V({file_marker(identifier)}).fold().coalesce("
        + _guard(
            "__.unfold()",
            f"__.hasLabel('ProjectionState').has('project_id',{literal(project_id)})",
            PROJECT_MISMATCH,
        )
        + ","
        f"__.addV('ProjectionState').property(T.id,{file_marker(identifier)})"
        f".property(single,'project_id',{literal(project_id)}))"
    )


def ordered_mutations(change):
    project = literal(change.project_id)
    sequence = change.sequence
    subject = change.snapshot_file_id
    marker = file_marker(subject) if subject else project_marker(change.project_id)
    if change.event_type == "ProjectDeleted":
        return (
            f"__.V({project_marker(change.project_id)}).fold().coalesce(__.unfold(),"
            "__.addV('ProjectionState')"
            f".property(T.id,{project_marker(change.project_id)})"
            f".property(single,'project_id',{project}))"
            ".property(single,'deleted',true)"
            f".sideEffect(__.V().hasLabel('File').has('project_id',{project}).drop())"
        )
    # Project and document IDs are never reused after permanent deletion.
    blocked = (
        f"__.or(__.V({project_marker(change.project_id)}).has('deleted',true),"
        f"__.V({marker}).has('deleted',true),"
        f"__.V({marker}).has('sequence',gte({sequence}L)))"
    )
    updates = (
        _state(subject, change.project_id) + f".property(single,'sequence',{sequence}L)"
    )
    if change.deleted_file_ids:
        updates += ".property(single,'deleted',true)"
        updates += (
            ".sideEffect("
            + _guard(
                f"__.V({_vertex(subject)})",
                _file_scope(change.project_id),
                PROJECT_MISMATCH,
            )
            + ".drop())"
        )
    elif change.trashed_file_ids:
        updates += (
            ".sideEffect("
            + _guard(
                f"__.V({_vertex(subject)})",
                _file_scope(change.project_id),
                PROJECT_MISMATCH,
            )
            + ".property(single,'trashed',true))"
        )
    else:
        file = change.files[0]
        node = (
            f"__.V({_vertex(file.id)}).fold().coalesce("
            + _guard("__.unfold()", _file_scope(change.project_id), PROJECT_MISMATCH)
            + f",__.addV('File').property(T.id,{_vertex(file.id)})"
            f".property(single,'project_id',{project}))"
            + _properties(
                {
                    "title": file.title,
                    "folder_code": file.folder_code,
                    "description": file.description,
                    "trashed": file.trashed,
                },
                vertex=True,
            )
        )
        updates += f".sideEffect({node})"
        # A snapshot replaces only this document's older incident edges.
        keep = ",".join(_edge(relation.id) for relation in change.relations)
        missing = f".not(__.hasId({keep}))" if keep else ""
        updates += (
            f".sideEffect(__.V({_vertex(subject)}).bothE('REFERENCES')"
            f".has('project_id',{project}){missing}"
            f".or(__.hasNot('sequence'),__.has('sequence',lte({sequence}L))).drop())"
        )
        for relation in change.relations:
            low, high = relation.low_document_id, relation.high_document_id
            other = high if low == subject else low
            stale = (
                f"__.or(__.V({file_marker(other)}).has('deleted',true),"
                f"__.V({file_marker(other)}).has('sequence',gt({sequence}L)),"
                f"__.E({_edge(relation.id)}).has('sequence',gt({sequence}L)))"
            )
            edge = "__.identity()"
            # A not-yet-arrived endpoint is hidden until its own snapshot arrives.
            edge += (
                f".sideEffect(__.V({_vertex(other)}).fold().coalesce("
                + _guard(
                    "__.unfold()", _file_scope(change.project_id), PROJECT_MISMATCH
                )
                + f",__.addV('File').property(T.id,{_vertex(other)})"
                f".property(single,'project_id',{project}).property(single,'trashed',true)))"
            )
            # Content may delete and recreate the same pair with a new relation UUID.
            edge += (
                f".sideEffect(__.V({_vertex(low)}).bothE('REFERENCES')"
                f".has('project_id',{project}).not(__.hasId({_edge(relation.id)}))"
                f".filter(__.otherV().hasId({_vertex(high)})).drop())"
            )
            edge += (
                f".sideEffect(__.E({_edge(relation.id)}).fold().coalesce("
                + _guard(
                    "__.unfold()",
                    _edge_scope(change.project_id)
                    + f".filter(__.outV().hasId({_vertex(low)})).filter(__.inV().hasId({_vertex(high)}))",
                    PROJECT_MISMATCH,
                )
                + ","
                f"__.V({_vertex(low)}).addE('REFERENCES').to(__.V({_vertex(high)}))"
                f".property(T.id,{_edge(relation.id)}).property('project_id',{project}))"
                f".property('sequence',{sequence}L)"
                f".property('description',{literal(relation.description)}).property('origin','USER'))"
            )
            updates += f".sideEffect(__.choose({stale},__.identity(),{edge}))"
    return f"__.choose({blocked},__.identity(),{updates})"
