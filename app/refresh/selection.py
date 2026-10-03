"""불변 입력 검증과 명시적 관계 선택. 추가 조회는 ID 반환 뒤 서비스가 수행한다."""

from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentState,
    ExecutionSnapshot,
    Failure,
    Selection,
)
from app.refresh.policy import MAX_BODY_CHARS, MAX_CHANGED_DOCUMENTS, RELATION_KEYS


def reject(code, message):
    """재시도 불가능한 업무 실패를 반환 경계로 전달한다."""
    raise RefreshFailure(Failure(code, message))


def validate_document(request, document):
    """프로젝트·revision·분류와 ACTIVE 본문의 원문 한도를 검증한다."""
    if document.project_id != request.job.project_id:
        reject("PROJECT_MISMATCH", "Document belongs to another project")
    if (
        not document.document_id
        or type(document.revision_no) is not int
        or document.revision_no < 1
    ):
        reject("INVALID_DOCUMENT", "Document ID and positive revision required")
    if document.folder_code not in RELATION_KEYS or document.state not in set(
        DocumentState
    ):
        reject("INVALID_DOCUMENT", "Unknown document classification or state")
    for relation in document.relations:
        if (
            relation.document_id != document.document_id
            or relation.project_id != request.job.project_id
        ):
            reject("INVALID_RELATION", "Embedded relation owner or project differs")
        if (
            relation.relation_key not in RELATION_KEYS.values()
            or relation.document_id == relation.target_document_id
        ):
            reject("INVALID_RELATION", "Invalid embedded relationship")
    if document.state == DocumentState.ACTIVE:
        if not isinstance(document.body_text, str):
            reject("BODY_MISSING", "Active document requires body_text")
        if len(document.body_text) > request.settings.max_input_chars:
            reject("BODY_TOO_LARGE", "Document body exceeds codepoint limit")


def validate_input(request, source):
    """식별·중복·설정·입력 한도를 검사한다. 비활성 항목도 변경 개수에 포함한다."""
    settings = request.settings
    if source.job != request.job:
        reject("INPUT_MISMATCH", "Input identity differs from request")
    if not all(
        (request.job.project_id, request.job.request_id, request.job.input_fingerprint)
    ):
        reject("INPUT_MISMATCH", "Request identity must be nonempty")
    positive = (
        settings.max_changed_documents,
        settings.max_input_chars,
        settings.max_context_chars,
        settings.max_model_calls,
        settings.chunk_chars,
    )
    if (
        any(type(v) is not int or v <= 0 for v in positive)
        or type(settings.max_documents) is not int
        or settings.max_documents < 0
        or type(settings.max_depth) is not int
        or not 0 <= settings.max_depth <= 3
    ):
        reject("INVALID_POLICY", "Invalid execution budget")
    if (
        settings.max_changed_documents > MAX_CHANGED_DOCUMENTS
        or settings.max_input_chars > MAX_BODY_CHARS
        or not settings.policy_version
        or not set(settings.relation_keys) <= set(RELATION_KEYS.values())
    ):
        reject("INVALID_POLICY", "Policy exceeds supported MVP contract")
    if len(source.documents) > settings.max_changed_documents:
        reject("TOO_MANY_CHANGED_DOCUMENTS", "Changed document count exceeds limit")
    ids = [d.document_id for d in source.documents]
    if len(ids) != len(set(ids)):
        reject("DUPLICATE_DOCUMENT", "Duplicate changed document ID")
    if not set(request.seed_ids) <= set(ids):
        reject("SEED_MISSING", "Seed must be present in input")
    if request.changed_documents:
        manifest = {
            item.document_id: (item.revision_no, item.state)
            for item in request.changed_documents
        }
        actual = {
            item.document_id: (item.revision_no, item.state)
            for item in source.documents
        }
        if len(manifest) != len(request.changed_documents) or manifest != actual:
            reject("MANIFEST_MISMATCH", "Request manifest differs from S3 input")
    for document in source.documents:
        validate_document(request, document)


def relations_for(source, related):
    return tuple(r for d in source.documents for r in d.relations) + related.relations


def select(request, source, related):
    """입력 전체와 명시 대상·관계 도달 문서를 선택한다. 미연결 조회 ID는 무시한다."""
    validate_input(request, source)
    present = {d.document_id for d in source.documents}
    inactive = {
        d.document_id for d in source.documents if d.state != DocumentState.ACTIVE
    }
    selected = present | set(request.target_ids)
    frontier = set(request.seed_ids) - inactive
    visited = set(frontier)
    relations = relations_for(source, related)
    for relation in relations:
        if relation.project_id != request.job.project_id:
            reject("PROJECT_MISMATCH", "Relationship belongs to another project")
        if (
            relation.relation_key not in RELATION_KEYS.values()
            or relation.document_id == relation.target_document_id
        ):
            reject("INVALID_RELATION", "Invalid relationship key or self connection")
    for _ in range(request.settings.max_depth):
        following = set()
        for relation in relations:
            if (
                request.settings.relation_keys
                and relation.relation_key not in request.settings.relation_keys
            ):
                continue
            a, b = relation.document_id, relation.target_document_id
            if a in inactive or b in inactive:
                continue
            if a in frontier:
                following.add(b)
            if b in frontier:
                following.add(a)
        following -= visited
        selected |= following
        visited |= following
        frontier = following
    missing = selected - present
    if len(missing) > request.settings.max_documents:
        reject("REFERENCE_BUDGET_EXCEEDED", "Additional document budget exceeded")
    return Selection(tuple(sorted(selected)), tuple(sorted(missing)))


def assemble(request, source, related, selection, fetched):
    """요청한 추가 문서만 합치고 기본 입력을 최신 조회 값으로 덮어쓰지 않는다."""
    expected = select(request, source, related)
    if selection != expected:
        reject("SELECTION_MISMATCH", "Selection differs from deterministic policy")
    fetched_ids = [d.document_id for d in fetched.documents]
    if len(fetched_ids) != len(set(fetched_ids)) or set(fetched_ids) - set(
        selection.missing_ids
    ):
        reject("FETCH_MISMATCH", "Unexpected or duplicate fetched document")
    if fetched.missing_ids or set(fetched_ids) != set(selection.missing_ids):
        reject("DOCUMENT_MISSING", "Required document unavailable")
    for document in fetched.documents:
        validate_document(request, document)
        if document.state != DocumentState.ACTIVE:
            reject("DOCUMENT_INACTIVE", "Required reference is not active")
    documents = tuple(
        sorted(source.documents + fetched.documents, key=lambda d: d.document_id)
    )
    by_id = {d.document_id: d for d in documents}
    all_relations = related.relations + tuple(r for d in documents for r in d.relations)
    for relation in all_relations:
        if relation.document_id in by_id and relation.target_document_id in by_id:
            if (
                relation.relation_key
                != RELATION_KEYS[by_id[relation.target_document_id].folder_code]
            ):
                reject(
                    "INVALID_RELATION", "Relationship key differs from target category"
                )
    explicit = set(request.target_ids)
    if any(
        by_id[i].state != DocumentState.ACTIVE or by_id[i].folder_code == "MANUSCRIPT"
        for i in explicit
    ):
        reject("INVALID_TARGET", "Targets must be active existing settings")
    targets = explicit or {
        d.document_id
        for d in documents
        if d.state == DocumentState.ACTIVE and d.folder_code != "MANUSCRIPT"
    }
    return ExecutionSnapshot(
        request,
        documents,
        related,
        tuple(sorted(targets)),
        tuple(sorted(d.document_id for d in source.documents)),
    )
