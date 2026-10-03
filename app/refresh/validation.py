"""생성 후보를 원문 스냅샷과 대조한다. 인용의 의미적 함의나 모델 품질은 보증하지 않는다."""

from dataclasses import replace

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import unique_evidence
from app.refresh.models import (
    DocumentProposal,
    DocumentState,
    Evidence,
    ModelCandidate,
    Outcome,
    RefreshResult,
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
        if not isinstance(item, Evidence):
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
        if not isinstance(proposal, DocumentProposal):
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
        if previous is not None:
            if previous.value != proposal.value:
                reject(
                    "CONFLICTING_PROPOSALS",
                    "Different values for the same target field",
                )
            evidence = unique_evidence(previous.evidence + evidence)
        merged[key] = replace(proposal, evidence=evidence)
    return tuple(
        merged[key]
        for key in sorted(merged)
        if merged[key].value != documents[key[0]].body_text
    )


def validate_relation_proposals(snapshot, proposals):
    """방향별 분류 키를 검증한 뒤 역방향 중복·기존 연결을 제거한다."""
    if not isinstance(proposals, tuple):
        reject("INVALID_RELATION_PROPOSAL", "Relation proposal tuple required")
    documents = {d.document_id: d for d in snapshot.documents}
    existing = {
        tuple(sorted((r.document_id, r.target_document_id)))
        for r in snapshot.related.relations
        + tuple(r for d in snapshot.documents for r in d.relations)
    }
    merged = {}
    for proposal in proposals:
        if not isinstance(proposal, RelationProposal):
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
        if (
            type(proposal.base_revision_no) is not int
            or type(proposal.target_base_revision_no) is not int
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
        if previous is not None:
            if previous.description != proposal.description:
                reject(
                    "CONFLICTING_PROPOSALS",
                    "Different descriptions for the same new connection",
                )
            evidence = unique_evidence(previous.evidence + evidence)
        merged[key] = replace(proposal, evidence=evidence)
    return tuple(merged[key] for key in sorted(merged))


def validate_candidate(snapshot, candidate, model_input):
    """하나라도 잘못되면 두 목록을 비운 FAILED를 반환한다. 유효 빈 결과는 NO_CHANGE다."""
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
        allowed = model_input.target_ids or snapshot.target_ids
        if not set(allowed) <= set(snapshot.target_ids):
            reject("INVALID_TARGET", "Model scope differs from execution scope")
        documents = validate_document_proposals(
            snapshot, candidate.document_proposals, allowed
        )
        relations = validate_relation_proposals(snapshot, candidate.relation_proposals)
        return RefreshResult(
            snapshot.request.job,
            Outcome.PROPOSED if documents or relations else Outcome.NO_CHANGE,
            documents,
            usage,
            prompt_version=model_input.prompt_version,
            relation_proposals=relations,
        )
    except RefreshFailure as error:
        return RefreshResult(
            snapshot.request.job,
            Outcome.FAILED,
            usage=usage,
            failure=error.failure,
            prompt_version=model_input.prompt_version,
        )
