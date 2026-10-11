"""생성 후보를 원문 스냅샷과 대조한다. 인용의 의미적 함의나 모델 품질은 보증하지 않는다."""

from dataclasses import dataclass, replace
from unicodedata import normalize

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import unique_evidence
from app.refresh.facts import validate_facts
from app.refresh.models import (
    DocumentProposal,
    DocumentState,
    Evidence,
    ModelCandidate,
    NewDocumentProposal,
    Outcome,
    RefreshResult,
    RelationGenerationScope,
    RelationProposal,
    Usage,
)
from app.refresh.policy import ALLOWED_FIELDS, RELATION_KEYS
from app.refresh.selection import reject


def validate_evidence(snapshot, evidence):
    """근거의 ID·revision·Unicode 구간·인용을 원본과 정확히 대조한다."""
    if not isinstance(evidence, tuple) or not evidence:
        reject("INVALID_EVIDENCE", "Nonempty evidence tuple required")
    documents = {d.document_id: d for d in snapshot.documents}
    for item in evidence:
        if (
            not isinstance(item, Evidence)
            or not isinstance(item.document_id, str)
            or not isinstance(item.quote, str)
        ):
            reject("INVALID_EVIDENCE", "Evidence value required")
        document = documents.get(item.document_id)
        if (
            document is None
            or document.state != DocumentState.ACTIVE
            or document.project_id != snapshot.request.job.project_id
        ):
            reject("INVALID_EVIDENCE", "Evidence source unavailable")
        if (
            type(item.revision_no) is not int
            or item.revision_no != document.revision_no
        ):
            reject("INVALID_EVIDENCE", "Evidence revision differs")
        if (
            type(item.start) is not int
            or type(item.end) is not int
            or not 0 <= item.start < item.end <= len(document.body_text)
        ):
            reject("INVALID_EVIDENCE", "Evidence span invalid")
        if item.quote != document.body_text[item.start : item.end]:
            reject("INVALID_EVIDENCE", "Evidence quote differs from original")
    return unique_evidence(evidence)


def validate_document_proposals(snapshot, proposals, allowed_targets=None):
    """허용 대상·필드·기준 revision을 검사하고 중복을 합친 뒤 동일 본문을 제외한다."""
    if not isinstance(proposals, tuple):
        reject("INVALID_DOCUMENT_PROPOSAL", "Proposal tuple required")
    documents = {d.document_id: d for d in snapshot.documents}
    allowed = set(snapshot.target_ids if allowed_targets is None else allowed_targets)
    merged = {}
    for proposal in proposals:
        if (
            not isinstance(proposal, DocumentProposal)
            or not isinstance(proposal.target_document_id, str)
            or not isinstance(proposal.field, str)
        ):
            reject("INVALID_DOCUMENT_PROPOSAL", "DocumentProposal required")
        document = documents.get(proposal.target_document_id)
        if (
            document is None
            or document.document_id not in allowed
            or document.project_id != snapshot.request.job.project_id
            or document.state != DocumentState.ACTIVE
            or document.folder_code == "MANUSCRIPT"
        ):
            reject("INVALID_TARGET", "Proposal target outside active settings scope")
        if (
            type(proposal.base_revision_no) is not int
            or proposal.base_revision_no != document.revision_no
        ):
            reject("REVISION_MISMATCH", "Proposal base revision differs")
        if (
            proposal.field not in ALLOWED_FIELDS
            or not isinstance(proposal.value, str)
            or len(proposal.value) > snapshot.request.settings.max_input_chars
        ):
            reject("INVALID_FIELD", "Only bounded body_text replacement is allowed")
        evidence = validate_evidence(snapshot, proposal.evidence)
        key = (proposal.target_document_id, proposal.field)
        previous = merged.get(key)
        fact_ids = proposal.fact_ids
        if previous is not None:
            if previous.value != proposal.value:
                reject(
                    "CONFLICTING_PROPOSALS",
                    "Different values for the same target field",
                )
            evidence = unique_evidence(previous.evidence + evidence)
            fact_ids = tuple(dict.fromkeys(previous.fact_ids + fact_ids))
        merged[key] = replace(proposal, evidence=evidence, fact_ids=fact_ids)
    return tuple(
        merged[key]
        for key in sorted(merged)
        if merged[key].value != documents[key[0]].body_text
    )


def setting_name(value):
    """이름 중복 검사에만 정규화를 사용하며 원문 본문은 변경하지 않는다."""
    return " ".join(normalize("NFKC", value).split()).casefold()


def existing_setting_names(snapshot):
    """같은 프로젝트의 활성 설정 제목·이름·단일 별칭을 분류별로 수집한다.

    Content의 alias는 문자열 속성이다. 구분자나 별칭 배열을 추정하지 않으며
    다른 설정의 본문에 등장한 이름도 독립 문서의 이름으로 취급하지 않는다.
    """
    names = set()
    for document in snapshot.documents:
        if (
            document.project_id != snapshot.request.job.project_id
            or document.state != DocumentState.ACTIVE
            or document.folder_code == "MANUSCRIPT"
        ):
            continue
        values = [document.title]
        values.extend(
            value
            for key, value in document.properties
            if key in {"name", "title", "alias"}
        )
        names.update(
            (document.folder_code, setting_name(value))
            for value in values
            if isinstance(value, str) and value.strip()
        )
    return names


def validate_new_document_proposals(snapshot, proposals):
    """원고에 근거한 새 설정, 임시 참조, 중복·충돌과 생성 개수 한도를 검사한다."""
    if not isinstance(proposals, tuple):
        reject("INVALID_NEW_DOCUMENT", "New setting proposal tuple required")
    documents = {d.document_id: d for d in snapshot.documents}
    existing_names = existing_setting_names(snapshot)
    merged, names = {}, {}
    for proposal in proposals:
        if (
            not isinstance(proposal, NewDocumentProposal)
            or not isinstance(proposal.folder_code, str)
            or proposal.folder_code not in RELATION_KEYS
            or proposal.folder_code == "MANUSCRIPT"
            or not isinstance(proposal.name, str)
            or not proposal.name.strip()
            or proposal.name != proposal.name.strip()
            or len(proposal.name) > 200
            or not isinstance(proposal.body_text, str)
            or not proposal.body_text.strip()
            or len(proposal.body_text) > snapshot.request.settings.max_input_chars
            or not isinstance(proposal.candidate_id, str)
            or proposal.candidate_id != f"new:{proposal.folder_code}:{proposal.name}"
            or proposal.candidate_id in documents
        ):
            reject(
                "INVALID_NEW_DOCUMENT",
                "Bounded setting with canonical candidate ID required",
            )
        name = (proposal.folder_code, setting_name(proposal.name))
        if name in existing_names:
            reject("DUPLICATE_SETTING", "Setting already exists in execution context")
        if name in names and names[name] != proposal.candidate_id:
            reject("DUPLICATE_SETTING", "Multiple candidates name the same setting")
        names[name] = proposal.candidate_id
        evidence = validate_evidence(snapshot, proposal.evidence)
        if not any(
            e.document_id in snapshot.changed_ids
            and documents[e.document_id].folder_code == "MANUSCRIPT"
            for e in evidence
        ):
            reject(
                "INVALID_EVIDENCE", "New setting requires changed manuscript evidence"
            )
        previous = merged.get(proposal.candidate_id)
        fact_ids = proposal.fact_ids
        if previous is not None:
            if (previous.folder_code, previous.name, previous.body_text) != (
                proposal.folder_code,
                proposal.name,
                proposal.body_text,
            ):
                reject(
                    "CONFLICTING_PROPOSALS",
                    "Different content for the same new setting",
                )
            evidence = unique_evidence(previous.evidence + evidence)
            fact_ids = tuple(dict.fromkeys(previous.fact_ids + fact_ids))
        merged[proposal.candidate_id] = replace(
            proposal, evidence=evidence, fact_ids=fact_ids
        )
        if len(merged) > 20:
            reject("INVALID_NEW_DOCUMENT", "At most 20 new settings per request")
    return tuple(merged[key] for key in sorted(merged))


@dataclass(frozen=True)
class RelationEndpoint:
    """관계 검증용 식별 정보. 새 후보의 revision은 미확정이므로 None이다."""

    document_id: str
    folder_code: str
    revision_no: int | None
    project_id: str
    state: DocumentState = DocumentState.ACTIVE


def relation_in_scope(snapshot, proposal, scope):
    """방향에 무관하게 기존 대상 중 첫 endpoint, 없거나 신규면 fallback 호출을 고른다.

    scope가 None이면 과거 단일 호출 또는 전체 후보 집계의 제약 없는 검증이다.
    이 함수는 소유권만 판단하며 endpoint의 실제 존재와 활성 상태는 별도 검증한다.
    """
    if scope is None:
        return True
    endpoints = {proposal.document_id, proposal.target_document_id}
    if not endpoints <= {document.document_id for document in snapshot.documents}:
        return scope.owns_fallback
    owner = next(
        (target for target in snapshot.target_ids if target in endpoints), None
    )
    return scope.owns_fallback if owner is None else owner in scope.owner_target_ids


def validate_relation_proposals(
    snapshot, proposals, new_documents=(), relation_scope=None
):
    """방향별 분류 키를 검증한 뒤 역방향 중복·기존 연결을 제거한다."""
    if not isinstance(proposals, tuple):
        reject("INVALID_RELATION_PROPOSAL", "Relation proposal tuple required")
    documents = {d.document_id: d for d in snapshot.documents}
    documents.update(
        {
            p.candidate_id: RelationEndpoint(
                p.candidate_id, p.folder_code, None, snapshot.request.job.project_id
            )
            for p in new_documents
        }
    )
    existing = {
        tuple(sorted((r.document_id, r.target_document_id)))
        for r in snapshot.related.relations
        + tuple(r for d in snapshot.documents for r in d.relations)
    }
    merged = {}
    for proposal in proposals:
        if (
            not isinstance(proposal, RelationProposal)
            or not isinstance(proposal.document_id, str)
            or not isinstance(proposal.target_document_id, str)
        ):
            reject("INVALID_RELATION_PROPOSAL", "RelationProposal required")
        a, b = (
            documents.get(proposal.document_id),
            documents.get(proposal.target_document_id),
        )
        if (
            a is None
            or b is None
            or a.document_id == b.document_id
            or any(
                d.project_id != snapshot.request.job.project_id
                or d.state != DocumentState.ACTIVE
                for d in (a, b)
            )
        ):
            reject(
                "INVALID_RELATION_ENDPOINT",
                "Endpoints must be distinct active project documents",
            )
        if (
            proposal.operation != "ADD"
            or proposal.relation_key != RELATION_KEYS.get(b.folder_code)
            or proposal.reverse_relation_key != RELATION_KEYS.get(a.folder_code)
        ):
            reject(
                "INVALID_RELATION_KEY",
                "ADD with correctly directed category keys required",
            )
        if not relation_in_scope(snapshot, proposal, relation_scope):
            reject(
                "INVALID_RELATION_SCOPE",
                "Connection is owned by another model call",
            )
        if (
            (a.revision_no is not None and type(proposal.base_revision_no) is not int)
            or (
                b.revision_no is not None
                and type(proposal.target_base_revision_no) is not int
            )
            or proposal.base_revision_no != a.revision_no
            or proposal.target_base_revision_no != b.revision_no
        ):
            reject("REVISION_MISMATCH", "Relation endpoint revision differs")
        if (
            not isinstance(proposal.description, str)
            or not proposal.description.strip()
            or len(proposal.description) > snapshot.request.settings.max_input_chars
        ):
            reject(
                "INVALID_RELATION_DESCRIPTION",
                "Bounded nonempty relation description required",
            )
        evidence = validate_evidence(snapshot, proposal.evidence)
        key = tuple(sorted((a.document_id, b.document_id)))
        if key in existing:
            continue
        if a.document_id > b.document_id:
            proposal = replace(
                proposal,
                document_id=b.document_id,
                target_document_id=a.document_id,
                base_revision_no=b.revision_no,
                target_base_revision_no=a.revision_no,
                relation_key=proposal.reverse_relation_key,
                reverse_relation_key=proposal.relation_key,
            )
        previous = merged.get(key)
        fact_ids = proposal.fact_ids
        if previous is not None:
            if previous.description != proposal.description:
                reject(
                    "CONFLICTING_PROPOSALS",
                    "Different descriptions for the same new connection",
                )
            evidence = unique_evidence(previous.evidence + evidence)
            fact_ids = tuple(dict.fromkeys(previous.fact_ids + fact_ids))
        merged[key] = replace(proposal, evidence=evidence, fact_ids=fact_ids)
    return tuple(merged[key] for key in sorted(merged))


def validate_candidate(snapshot, candidate, model_input):
    """하나라도 잘못되면 세 목록을 비운 FAILED를 반환한다. 유효 빈 결과는 NO_CHANGE다."""
    usage = Usage()
    try:
        if not isinstance(candidate, ModelCandidate) or not isinstance(
            candidate.usage, Usage
        ):
            reject("INVALID_MODEL_RESPONSE", "Typed model candidate required")
        if any(
            type(v) is not int or v < 0
            for v in (
                candidate.usage.calls,
                candidate.usage.input_tokens,
                candidate.usage.output_tokens,
            )
        ):
            reject("INVALID_USAGE", "Nonnegative integer usage required")
        usage = candidate.usage
        if candidate.new_document_proposals and not model_input.allow_new_documents:
            reject("INVALID_NEW_DOCUMENT", "New settings are outside this call's scope")
        if type(model_input.allow_document_updates) is not bool:
            reject("INVALID_TARGET", "Document update scope must be a boolean")
        allowed = (
            (model_input.target_ids or snapshot.target_ids)
            if model_input.allow_document_updates
            else ()
        )
        if not set(allowed) <= set(snapshot.target_ids):
            reject("INVALID_TARGET", "Model scope differs from execution scope")
        scope = model_input.relation_scope
        if scope is not None and (
            not isinstance(scope, RelationGenerationScope)
            or not isinstance(scope.owner_target_ids, tuple)
            or not all(isinstance(target, str) for target in scope.owner_target_ids)
            or scope.owner_target_ids != model_input.target_ids
            or len(scope.owner_target_ids) != len(set(scope.owner_target_ids))
            or type(scope.owns_fallback) is not bool
        ):
            reject("INVALID_RELATION_SCOPE", "Invalid model connection ownership")
        if scope is not None:
            if snapshot.request.settings.separate_discovery:
                discovery = not model_input.allow_document_updates
                if (
                    scope.owns_fallback != discovery
                    or (not scope.owner_target_ids) != discovery
                    or model_input.allow_new_documents != discovery
                ):
                    reject(
                        "INVALID_RELATION_SCOPE",
                        "Discovery and update calls have different connection owners",
                    )
            elif scope.owns_fallback != (
                not snapshot.target_ids
                or snapshot.target_ids[0] in scope.owner_target_ids
            ):
                reject("INVALID_RELATION_SCOPE", "Invalid fallback connection owner")
        documents = validate_document_proposals(
            snapshot, candidate.document_proposals, allowed
        )
        new_documents = validate_new_document_proposals(
            snapshot, candidate.new_document_proposals
        )
        if not isinstance(candidate.relation_proposals, tuple):
            reject("INVALID_RELATION_PROPOSAL", "Relation proposal tuple required")
        facts = validate_facts(
            snapshot,
            candidate,
            validate_evidence,
            required=model_input.schema_version
            in {"refresh-candidate-v4", "refresh-candidate-v5"},
            schema_version=model_input.schema_version,
            aggregate=model_input.relation_scope is None,
        )
        relations = validate_relation_proposals(
            snapshot, candidate.relation_proposals, new_documents, scope
        )
        return RefreshResult(
            snapshot.request.job,
            Outcome.PROPOSED
            if documents or relations or new_documents
            else Outcome.NO_CHANGE,
            documents,
            usage,
            prompt_version=model_input.prompt_version,
            relation_proposals=relations,
            new_document_proposals=new_documents,
            facts=tuple(
                facts[key]
                for key in sorted(
                    {
                        key
                        for proposal in documents + relations + new_documents
                        for key in proposal.fact_ids
                    }
                )
            ),
            candidate_schema_version=model_input.schema_version,
        )
    except RefreshFailure as error:
        return RefreshResult(
            snapshot.request.job,
            Outcome.FAILED,
            usage=usage,
            failure=error.failure,
            prompt_version=model_input.prompt_version,
            candidate_schema_version=model_input.schema_version,
        )
