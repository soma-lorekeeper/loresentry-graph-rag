"""모델 후보의 전송 스키마와 엄격한 내부 값 변환. 업무 판단은 rules가 맡는다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentProposal,
    Evidence,
    Failure,
    ModelCandidate,
    NewDocumentProposal,
    RelationProposal,
    Usage,
)

CANDIDATE_VERSION = "refresh-candidate-v2"
PROMPT_VERSION = "refresh-prompt-v3"


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
    """S3 결과 계약과 독립적인 모델 응답 계약."""

    document_proposals: list[DocumentDTO]
    relation_proposals: list[RelationDTO]
    new_document_proposals: list[NewDocumentDTO]


class LegacyRelationDTO(RelationDTO):
    """저장된 v1 후보에서는 양 끝이 기존 문서다."""

    base_revision_no: int
    target_base_revision_no: int


class LegacyCandidateDTO(StrictDTO):
    """과거 평가 자료 읽기 전용. 실제 생성 요청에는 v2를 사용한다."""

    document_proposals: list[DocumentDTO]
    relation_proposals: list[LegacyRelationDTO]


def candidate_schema() -> dict:
    """Responses의 strict JSON schema 형식에 전달할 스키마를 만든다."""
    return CandidateDTO.model_json_schema()


def response_usage(payload: dict | None) -> Usage:
    """총량만 매핑한다. 누락·잘못된 사용량을 무료 호출로 기록하지 않는다."""
    if not isinstance(payload, dict) or any(
        type(payload.get(k)) is not int or payload[k] < 0
        for k in ("input_tokens", "output_tokens")
    ):
        raise RefreshFailure(Failure("MODEL_INVALID_RESPONSE", "Invalid model usage"))
    return Usage(1, payload["input_tokens"], payload["output_tokens"])


def parse_candidate(
    text: str, usage: Usage, *, schema_version=CANDIDATE_VERSION
) -> ModelCandidate:
    """JSON 후보를 불변 내부 값으로 변환하며 실패를 빈 제안으로 바꾸지 않는다."""
    try:
        if schema_version == "refresh-candidate-v1":
            dto = LegacyCandidateDTO.model_validate_json(text)
        elif schema_version == CANDIDATE_VERSION:
            dto = CandidateDTO.model_validate_json(text)
        else:
            raise ValueError("Unsupported candidate schema")
    except (ValidationError, ValueError):
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", "Invalid model candidate")
        ) from None

    def values(item: DocumentDTO | RelationDTO | NewDocumentDTO) -> dict:
        data = item.model_dump(exclude={"evidence"})
        data["evidence"] = tuple(Evidence(**e.model_dump()) for e in item.evidence)
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
