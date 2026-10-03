"""생성 후보를 원문 스냅샷과 대조한다. 인용의 의미적 함의나 모델 품질은 보증하지 않는다."""

from dataclasses import replace

from app.refresh.evidence import unique_evidence
from app.refresh.models import DocumentProposal, DocumentState, Evidence
from app.refresh.policy import ALLOWED_FIELDS
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
