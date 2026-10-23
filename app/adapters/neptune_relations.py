"""현재 File/REFERENCES 투영본에서 refresh용 활성 원홉 관계를 읽는다."""

from app.adapters.neptune_client import NeptuneGremlinClient, NeptuneUnavailable
from app.adapters.projection_gremlin import literal
from app.projection.models import canonical_uuid
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentState,
    Failure,
    Readiness,
    RefreshRequest,
    RelatedDocuments,
    Relation,
)
from app.refresh.policy import RELATION_KEYS


def _failure(code: str, message: str, *, retryable: bool = False):
    return RefreshFailure(Failure(code, message, retryable))


def _invalid(message: str):
    return _failure("GRAPH_RESPONSE_INVALID", message, retryable=True)


def _decode_id(value: object, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise _invalid("Graph element ID does not match the projection model")
    identifier = value[len(prefix) :]
    try:
        return canonical_uuid(identifier)
    except (ValueError, TypeError, AttributeError) as exc:
        raise _invalid("Graph element UUID is invalid") from exc


def _single_text(value: object) -> str:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        raise _invalid("Expected one current string property")
    return value[0]


def build_one_hop_query(
    project_id: str,
    seed_ids: tuple[str, ...],
    relation_keys: tuple[str, ...],
) -> str:
    """원홉만 읽는다. seed·edge·상대 노드 모두 같은 프로젝트의 활성 범위로 제한한다.

    이웃 수로 자르지 않는다. 모델 입력 크기는 토큰 예산과 묶음 처리가 다룬다.
    """
    identifiers = ",".join(literal(f"file:{seed}") for seed in seed_ids)
    project = literal(project_id)
    folders = [folder for folder, key in RELATION_KEYS.items() if key in relation_keys]
    category_filter = (
        ".has('folder_code',within(" + ",".join(map(literal, folders)) + "))"
        if relation_keys
        else ""
    )
    return (
        f"g.V({identifiers}).hasLabel('File').has('project_id',{project})"
        ".has('trashed',false).fold().project('seed_ids','relations')"
        ".by(__.unfold().id().fold())"
        ".by(__.unfold().as('seed').bothE('REFERENCES')"
        f".has('project_id',{project}).as('edge').otherV()"
        f".hasLabel('File').has('project_id',{project}).has('trashed',false)"
        f"{category_filter}"
        ".project('seed_id','target_id','relation_id','folder_code','description')"
        ".by(__.select('seed').id()).by(__.id())"
        ".by(__.select('edge').id()).by(__.values('folder_code').fold())"
        ".by(__.select('edge').values('description').fold()).fold())"
    )


class NeptuneRelatedDocumentSource(NeptuneGremlinClient):
    """RelatedDocumentSource의 실제 Neptune 구현. 본문은 Content 포트가 읽는다."""

    def find_related(self, request: RefreshRequest) -> RelatedDocuments:
        try:
            canonical_uuid(request.job.project_id)
            for seed in request.seed_ids:
                canonical_uuid(seed)
        except (ValueError, TypeError, AttributeError) as exc:
            raise _failure(
                "INVALID_GRAPH_REQUEST", "Canonical project/seed UUIDs required"
            ) from exc
        settings = request.settings
        if type(settings.max_depth) is not int or settings.max_depth not in (0, 1):
            raise _failure(
                "UNSUPPORTED_GRAPH_DEPTH", "Neptune refresh supports one hop"
            )
        if not set(settings.relation_keys) <= set(RELATION_KEYS.values()):
            raise _failure("INVALID_GRAPH_REQUEST", "Invalid graph selection policy")
        inactive = {
            document.document_id
            for document in request.changed_documents
            if document.state != DocumentState.ACTIVE
        }
        seeds = tuple(sorted(set(request.seed_ids) - inactive))
        if not seeds or settings.max_depth == 0:
            return RelatedDocuments()
        if request.required_graph_version is not None:
            # 현재 그래프에는 revision이 없다. 확인할 수 없는 준비 상태를 READY로 만들지 않는다.
            return RelatedDocuments(readiness=Readiness.UNKNOWN)
        query = build_one_hop_query(
            request.job.project_id, seeds, settings.relation_keys
        )
        try:
            result = self.execute(query)
        except NeptuneUnavailable as exc:
            raise _failure("GRAPH_QUERY_FAILED", str(exc), retryable=True) from exc
        return self._interpret(request, seeds, result)

    @staticmethod
    def _interpret(request, seeds, result):
        if len(result) != 1 or not isinstance(result[0], dict):
            raise _invalid("Expected one graph selection result")
        value = result[0]
        seed_values, rows = value.get("seed_ids"), value.get("relations")
        if not isinstance(seed_values, list) or not isinstance(rows, list):
            raise _invalid("Graph seeds and relations must be arrays")
        observed_seeds = [_decode_id(seed, "file:") for seed in seed_values]
        if len(observed_seeds) != len(set(observed_seeds)) or not set(
            observed_seeds
        ) <= set(seeds):
            raise _invalid("Graph returned unexpected seed IDs")
        relations = set()
        pairs = {}
        folders = {}
        for row in rows:
            if not isinstance(row, dict):
                raise _invalid("Graph relation must be an object")
            source = _decode_id(row.get("seed_id"), "file:")
            target = _decode_id(row.get("target_id"), "file:")
            relation_id = _decode_id(row.get("relation_id"), "rel:")
            folder = _single_text(row.get("folder_code"))
            description = _single_text(row.get("description"))
            if (
                source not in observed_seeds
                or source == target
                or folder not in RELATION_KEYS
            ):
                raise _invalid("Graph relation violates seed, pair or category rules")
            key = RELATION_KEYS[folder]
            if settings_keys := request.settings.relation_keys:
                if key not in settings_keys:
                    raise _invalid("Graph returned an excluded category")
            if target in folders and folders[target] != folder:
                raise _invalid("Graph returned conflicting document categories")
            folders[target] = folder
            pair = tuple(sorted((source, target)))
            identity = (relation_id, description)
            if pair in pairs and pairs[pair] != identity:
                raise _invalid(
                    "Graph contains conflicting copies of an undirected pair"
                )
            pairs[pair] = identity
            relations.add(
                Relation(request.job.project_id, source, target, key, description)
            )
        if set(observed_seeds) != set(seeds):
            return RelatedDocuments(readiness=Readiness.WAITING)
        document_ids = tuple(
            sorted({relation.target_document_id for relation in relations} - set(seeds))
        )
        return RelatedDocuments(
            document_ids=document_ids,
            relations=tuple(
                sorted(
                    relations, key=lambda rel: (rel.document_id, rel.target_document_id)
                )
            ),
            readiness=Readiness.READY,
            observed_version=None,
        )
