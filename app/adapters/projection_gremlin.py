"""Neptune HTTP 요청 하나에 Inbox와 모든 변경을 포함하는 Gremlin 생성."""

from app.projection.models import CONSUMER_NAME, ProjectionChange

INBOX_CONFLICT = "PROJECTION_INBOX_CONFLICT"
PROJECT_MISMATCH = "PROJECTION_PROJECT_MISMATCH"
MISSING_FILE = "PROJECTION_MISSING_FILE"
RELATION_CONFLICT = "PROJECTION_RELATION_CONFLICT"
PAIR_CONFLICT = "PROJECTION_PAIR_CONFLICT"
REJECTIONS = (
    INBOX_CONFLICT,
    PROJECT_MISMATCH,
    MISSING_FILE,
    RELATION_CONFLICT,
    PAIR_CONFLICT,
)


def literal(value: str | bool) -> str:
    """Bindings를 지원하지 않는 Neptune용 문자열·Boolean 리터럴."""
    if isinstance(value, bool):
        return "true" if value else "false"
    escapes = {
        "\\": "\\\\",
        "'": "\\'",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\b": "\\b",
        "\f": "\\f",
    }
    escaped = "".join(
        escapes.get(char, f"\\u{ord(char):04x}" if ord(char) < 32 else char)
        for char in value
    )
    return f"'{escaped}'"


def _file_scope(project_id: str) -> str:
    return f"__.hasLabel('File').has('project_id',{literal(project_id)})"


def _edge_scope(project_id: str) -> str:
    return (
        f"__.hasLabel('REFERENCES').has('project_id',{literal(project_id)})"
        f".filter(__.outV().filter({_file_scope(project_id)}))"
        f".filter(__.inV().filter({_file_scope(project_id)}))"
    )


def _guard(start: str, predicate: str, error: str) -> str:
    return f"{start}.choose({predicate},__.identity(),__.fail({literal(error)}))"


def _vertex(file_id: str) -> str:
    return literal(f"file:{file_id}")


def _edge(relation_id: str) -> str:
    return literal(f"rel:{relation_id}")


def _properties(properties: dict[str, str | bool], *, vertex: bool) -> str:
    cardinality = "single," if vertex else ""
    return "".join(
        f".property({cardinality}{literal(key)},{literal(value)})"
        for key, value in properties.items()
    )


def build_projection_query(change: ProjectionChange, processed_at: str) -> str:
    """중복 판정·변경·Inbox를 한 traversal로 묶고 불일치 시 전체를 롤백한다."""
    project_id = change.project_id
    inbox_properties = {
        "consumer_name": CONSUMER_NAME,
        "event_id": change.event_id,
        "project_id": project_id,
        "payload_sha256": change.payload_sha256,
    }
    matches = "__.hasLabel('InboxEvent')" + "".join(
        f".has({literal(key)},{literal(value)})"
        for key, value in inbox_properties.items()
    )
    duplicate = (
        f"__.unfold().choose({matches},__.constant('DUPLICATE'),"
        f"__.fail('{INBOX_CONFLICT}'))"
    )
    applied = (
        f"__.addV('InboxEvent').property(T.id,{literal(change.inbox_id)})"
        + _properties({**inbox_properties, "processed_at": processed_at}, vertex=True)
    )
    if change.sequence is not None:
        from app.adapters.ordered_projection import ordered_mutations

        applied += f".sideEffect({ordered_mutations(change)}).constant('APPLIED')"
        return f"g.V({literal(change.inbox_id)}).fold().coalesce({duplicate},{applied})"
    mutations: list[str] = []
    if change.event_type == "ProjectDeleted":
        mutations.append(
            _guard(
                "__.E().hasLabel('REFERENCES')"
                f".has('project_id',{literal(project_id)})",
                _edge_scope(project_id),
                PROJECT_MISMATCH,
            )
            + ".drop()"
        )
        mutations.append(
            _delete_file(
                f"__.V().hasLabel('File').has('project_id',{literal(project_id)})",
                project_id,
            )
        )
    else:
        for relation_id in change.deleted_relation_ids:
            mutations.append(
                _guard(
                    f"__.E({_edge(relation_id)})",
                    _edge_scope(project_id),
                    PROJECT_MISMATCH,
                )
                + ".drop()"
            )
        for file in change.files:
            existing = _guard("__.unfold()", _file_scope(project_id), PROJECT_MISMATCH)
            mutations.append(
                f"__.V({_vertex(file.id)}).fold().coalesce({existing},"
                f"__.addV('File').property(T.id,{_vertex(file.id)})"
                f".property(single,'project_id',{literal(project_id)}))"
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
        for file_id in change.deleted_file_ids:
            mutations.append(_delete_file(f"__.V({_vertex(file_id)})", project_id))
        for relation in change.relations:
            low, high = (
                _vertex(relation.low_document_id),
                _vertex(relation.high_document_id),
            )
            for endpoint in (low, high):
                mutations.append(
                    f"__.V({endpoint}).fold().coalesce("
                    + _guard("__.unfold()", _file_scope(project_id), PROJECT_MISMATCH)
                    + f",__.fail('{MISSING_FILE}'))"
                )
            # 다른 ID의 같은 무방향 쌍과 잘못 저장된 역방향 쌍도 거부한다.
            mutations.append(
                "__.choose("
                f"__.V({low}).bothE('REFERENCES')"
                f".not(__.hasId({_edge(relation.id)}))"
                f".filter(__.otherV().hasId({high})),"
                f"__.fail('{PAIR_CONFLICT}'),__.identity())"
            )
            matches_edge = (
                _edge_scope(project_id)
                + f".filter(__.outV().hasId({low}))"
                + f".filter(__.inV().hasId({high}))"
            )
            mutations.append(
                f"__.E({_edge(relation.id)}).fold().coalesce("
                + _guard("__.unfold()", matches_edge, RELATION_CONFLICT)
                + f",__.V({low}).addE('REFERENCES').to(__.V({high}))"
                f".property(T.id,{_edge(relation.id)})"
                f".property('project_id',{literal(project_id)}))"
                + _properties(
                    {"description": relation.description, "origin": "USER"},
                    vertex=False,
                )
            )
    applied += "".join(f".sideEffect({mutation})" for mutation in mutations)
    applied += ".constant('APPLIED')"
    return f"g.V({literal(change.inbox_id)}).fold().coalesce({duplicate},{applied})"


def _delete_file(start: str, project_id: str) -> str:
    return (
        _guard(start, _file_scope(project_id), PROJECT_MISMATCH)
        + ".sideEffect("
        + _guard("__.bothE()", _edge_scope(project_id), PROJECT_MISMATCH)
        + ").drop()"
    )
