"""모델 후보의 전송 스키마와 엄격한 내부 값 변환. 업무 판단은 rules가 맡는다."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentProposal,
    Evidence,
    Failure,
    ModelCandidate,
    ModelInput,
    NewDocumentProposal,
    RelationProposal,
    Usage,
)

CANDIDATE_VERSION = "refresh-candidate-v3"
PROMPT_VERSION = "refresh-prompt-v7"


class StrictDTO(BaseModel):
    """추가 필드와 묵시적 타입 변환을 금지하는 전송 객체."""

    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceDTO(StrictDTO):
    """원문 코드 포인트 구간. 실제 인용 일치는 rules에서 확인한다."""

    document_id: str
    revision_no: int
    start: int
    end: int
    quote: str


class DocumentDTO(StrictDTO):
    """기존 설정 본문 전체 교체 후보."""

    target_document_id: str
    base_revision_no: int
    field: Literal["body_text"]
    value: str
    evidence: list[EvidenceDTO]


class RelationDTO(StrictDTO):
    """기존 문서 또는 새 생성 후보를 연결하는 ADD 후보."""

    document_id: str
    target_document_id: str
    base_revision_no: int | None
    target_base_revision_no: int | None
    relation_key: str
    reverse_relation_key: str
    description: str
    evidence: list[EvidenceDTO]
    operation: Literal["ADD"]


class NewDocumentDTO(StrictDTO):
    """원고 근거를 갖춘 설정 문서 생성 후보."""

    candidate_id: str
    folder_code: Literal[
        "WORLDVIEW", "CHARACTER", "LOCATION", "ORGANIZATION", "ITEM", "EVENT"
    ]
    name: str
    body_text: str
    evidence: list[EvidenceDTO]


class CandidateDTO(StrictDTO):
    """저장된 v2 후보 읽기용 계약. 근거 원문·구간이 응답에 포함된다."""

    document_proposals: list[DocumentDTO]
    relation_proposals: list[RelationDTO]
    new_document_proposals: list[NewDocumentDTO]


class EvidenceMarkerDTO(StrictDTO):
    """모델은 서버가 제공한 출처 마커만 선택한다."""

    marker: str


class ContextEvidenceDTO(EvidenceDTO):
    """프롬프트 문맥에 서버가 기록한 마커와 원문 근거."""

    marker: str


class MarkerDocumentDTO(DocumentDTO):
    """기존 설정 후보의 마커 참조."""

    evidence: list[EvidenceMarkerDTO]


class MarkerRelationDTO(RelationDTO):
    """관계 후보의 마커 참조."""

    evidence: list[EvidenceMarkerDTO]


class MarkerNewDocumentDTO(NewDocumentDTO):
    """신규 설정 후보의 마커 참조."""

    evidence: list[EvidenceMarkerDTO]


class MarkerCandidateDTO(StrictDTO):
    """v3 생성 계약. 근거 위치·인용은 응답 대신 서버 문맥에서 확정한다."""

    document_proposals: list[MarkerDocumentDTO]
    relation_proposals: list[MarkerRelationDTO]
    new_document_proposals: list[MarkerNewDocumentDTO]


class LegacyRelationDTO(RelationDTO):
    """저장된 v1 후보에서는 양 끝이 기존 문서다."""

    base_revision_no: int
    target_base_revision_no: int


class LegacyCandidateDTO(StrictDTO):
    """과거 v1 평가 자료 읽기 전용."""

    document_proposals: list[DocumentDTO]
    relation_proposals: list[LegacyRelationDTO]


def candidate_schema() -> dict:
    """Responses의 strict JSON schema 형식에 전달할 스키마를 만든다."""
    return MarkerCandidateDTO.model_json_schema()


def context_evidence(model_input: ModelInput | None) -> dict[str, Evidence]:
    """중복이나 잘못된 마커를 거절한 뒤 서버 문맥의 정확한 근거를 복원한다."""
    if model_input is None:
        raise ValueError("Model context required")
    context = json.loads(model_input.prompt)
    if not isinstance(context, dict) or not isinstance(context.get("evidence"), list):
        raise ValueError("Evidence context required")
    indexed = {}
    for item in context["evidence"]:
        source = ContextEvidenceDTO.model_validate(item)
        if not source.marker or source.marker in indexed:
            raise ValueError("Evidence context marker invalid")
        indexed[source.marker] = Evidence(**source.model_dump(exclude={"marker"}))
    return indexed


def response_usage(payload: dict | None) -> Usage:
    """총량만 매핑한다. 누락·잘못된 사용량을 무료 호출로 기록하지 않는다."""
    if not isinstance(payload, dict) or any(
        type(payload.get(k)) is not int or payload[k] < 0
        for k in ("input_tokens", "output_tokens")
    ):
        raise RefreshFailure(Failure("MODEL_INVALID_RESPONSE", "Invalid model usage"))
    return Usage(1, payload["input_tokens"], payload["output_tokens"])


def parse_candidate(
    text: str,
    usage: Usage,
    *,
    schema_version=CANDIDATE_VERSION,
    model_input: ModelInput | None = None,
) -> ModelCandidate:
    """JSON 후보를 불변 내부 값으로 변환하며 실패를 빈 제안으로 바꾸지 않는다."""
    try:
        if schema_version == "refresh-candidate-v1":
            dto = LegacyCandidateDTO.model_validate_json(text)
        elif schema_version == "refresh-candidate-v2":
            dto = CandidateDTO.model_validate_json(text)
        elif schema_version == CANDIDATE_VERSION:
            dto = MarkerCandidateDTO.model_validate_json(text)
        else:
            raise ValueError("Unsupported candidate schema")
    except (ValidationError, ValueError):
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", "Invalid model candidate")
        ) from None

    proposals = (
        dto.document_proposals
        + dto.relation_proposals
        + getattr(dto, "new_document_proposals", [])
    )
    try:
        sources = (
            context_evidence(model_input)
            if schema_version == CANDIDATE_VERSION
            and (proposals or model_input is not None)
            else {}
        )
        if schema_version == CANDIDATE_VERSION and any(
            evidence.marker not in sources
            for item in proposals
            for evidence in item.evidence
        ):
            raise ValueError("Unknown evidence marker")
    except (ValidationError, ValueError, TypeError):
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", "Invalid model candidate evidence")
        ) from None

    def values(item) -> dict:
        data = item.model_dump(exclude={"evidence"})
        data["evidence"] = tuple(
            sources[e.marker]
            if schema_version == CANDIDATE_VERSION
            else Evidence(**e.model_dump())
            for e in item.evidence
        )
        return data

    return ModelCandidate(
        tuple(DocumentProposal(**values(item)) for item in dto.document_proposals),
        usage,
        tuple(RelationProposal(**values(item)) for item in dto.relation_proposals),
        tuple(
            NewDocumentProposal(**values(item))
            for item in getattr(dto, "new_document_proposals", ())
        ),
    )
