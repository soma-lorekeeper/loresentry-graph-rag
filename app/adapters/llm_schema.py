"""모델 후보의 전송 스키마와 엄격한 내부 값 변환. 업무 판단은 rules가 맡는다."""

import json
from dataclasses import asdict, replace
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import unique_evidence
from app.refresh.facts import fact_description
from app.refresh.models import (
    DocumentProposal,
    Evidence,
    FactEntity,
    Failure,
    ModelCandidate,
    ModelInput,
    NewDocumentProposal,
    RelationProposal,
    SourceFact,
    Usage,
    ValidationDiagnostic,
)
from app.refresh.prompt_versions import PROMPT_VERSION as PROMPT_VERSION

CANDIDATE_VERSION = "refresh-candidate-v5"
CONFIDENCE_CANDIDATE_VERSION = "refresh-candidate-v6"
GENERATION_VERSIONS = {
    CANDIDATE_VERSION,
    CONFIDENCE_CANDIDATE_VERSION,
    "refresh-plan-v1",
    "refresh-candidate-v7",
    "refresh-candidate-v8",
}
MARKER_VERSIONS = {"refresh-candidate-v3", "refresh-candidate-v4", *GENERATION_VERSIONS}


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


class FactEntityDTO(StrictDTO):
    """원문에 실제 등장하는 역할 표현과 명시적으로 확인한 설정 문서 ID."""

    surface: str
    document_id: str | None


class FactSourceDTO(StrictDTO):
    """서버 마커 안에서 그대로 선택한 짧은 구간. 오프셋은 서버가 계산한다."""

    marker: str
    excerpt: str


class SourceFactDTO(StrictDTO):
    fact_id: str
    actor: FactEntityDTO | None
    action: str
    recipient: FactEntityDTO | None
    object: FactEntityDTO | None
    certainty: Literal["CONFIRMED", "REPORTED", "PLANNED", "UNKNOWN"]
    extent: Literal["PARTIAL", "COMPLETE", "UNSPECIFIED"]
    time: Literal["CURRENT", "PAST", "FUTURE", "UNSPECIFIED"]
    statement: str
    source: FactSourceDTO


class MarkerSourceFactDTO(SourceFactDTO):
    """현행 사실의 원문은 서버 마커에서 복원한다."""

    source: EvidenceMarkerDTO


class FactDocumentDTO(MarkerDocumentDTO):
    fact_ids: list[str]


class FactNewDocumentDTO(MarkerNewDocumentDTO):
    fact_ids: list[str]


class FactRelationDTO(StrictDTO):
    """설명과 근거를 별도로 생성하지 않고 참조 사실에서 서버가 조립한다."""

    document_id: str
    target_document_id: str
    base_revision_no: int | None
    target_base_revision_no: int | None
    relation_key: str
    reverse_relation_key: str
    operation: Literal["ADD"]
    fact_ids: list[str]


class FactCandidateDTO(StrictDTO):
    facts: list[MarkerSourceFactDTO]
    document_proposals: list[FactDocumentDTO]
    relation_proposals: list[FactRelationDTO]
    new_document_proposals: list[FactNewDocumentDTO]


class ConfidenceSourceFactDTO(MarkerSourceFactDTO):
    """평가용 자기평가이며 실제 정답 확률로 보정된 값이 아니다."""

    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ConfidenceCandidateDTO(FactCandidateDTO):
    facts: list[ConfidenceSourceFactDTO]


class ExcerptFactCandidateDTO(FactCandidateDTO):
    """과거 v4 후보의 정확한 발췌 읽기 계약."""

    facts: list[SourceFactDTO]


class LegacyRelationDTO(RelationDTO):
    """저장된 v1 후보에서는 양 끝이 기존 문서다."""

    base_revision_no: int
    target_base_revision_no: int


class LegacyCandidateDTO(StrictDTO):
    """과거 v1 평가 자료 읽기 전용."""

    document_proposals: list[DocumentDTO]
    relation_proposals: list[LegacyRelationDTO]


def candidate_schema(schema_version=CANDIDATE_VERSION) -> dict:
    """Responses의 strict JSON schema 형식에 전달할 스키마를 만든다."""
    if schema_version in {
        "refresh-plan-v1",
        "refresh-candidate-v7",
        "refresh-candidate-v8",
    }:
        from app.adapters.refresh_workflow_schema import response_dto

        return response_dto(schema_version).model_json_schema()
    if schema_version == CONFIDENCE_CANDIDATE_VERSION:
        return ConfidenceCandidateDTO.model_json_schema()
    if schema_version != CANDIDATE_VERSION:
        raise ValueError("Unsupported generation schema")
    return FactCandidateDTO.model_json_schema()


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


def diagnostic_path(location) -> str:
    """DTO 위치를 보완 응답이 찾아갈 수 있는 필드 경로로 표현한다."""
    path = ""
    for part in location:
        path += (
            f"[{part}]" if isinstance(part, int) else ("." if path else "") + str(part)
        )
    return path or "$"


def diagnostic_value(value) -> str | None:
    """잘못된 필드 값만 짧게 보존하며 전체 응답·공급자 오류를 복사하지 않는다."""
    return str(value)[:200] if isinstance(value, (str, int, float, bool)) else None


def schema_diagnostics(error: ValidationError, text: str):
    """Pydantic 오류의 위치·유형만 전달하고 오류 문자열·공급자 본문은 노출하지 않는다."""
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        raw = None
    diagnostics = []
    for item in error.errors(include_url=False, include_context=False):
        location = item["loc"]
        fact = None
        if (
            len(location) > 1
            and location[0] == "facts"
            and isinstance(location[1], int)
            and isinstance(raw, dict)
            and isinstance(raw.get("facts"), list)
            and location[1] < len(raw["facts"])
            and isinstance(raw["facts"][location[1]], dict)
        ):
            fact = raw["facts"][location[1]]
        source = fact.get("source") if fact is not None else None
        diagnostics.append(
            ValidationDiagnostic(
                diagnostic_path(location),
                f"schema.{item['type']}",
                fact.get("fact_id")
                if fact is not None and isinstance(fact.get("fact_id"), str)
                else None,
                source.get("marker")
                if isinstance(source, dict) and isinstance(source.get("marker"), str)
                else None,
                diagnostic_value(item.get("input")) if location else None,
            )
        )
    return tuple(diagnostics)


def parse_candidate(
    text: str,
    usage: Usage,
    *,
    schema_version=CANDIDATE_VERSION,
    model_input: ModelInput | None = None,
) -> ModelCandidate:
    """JSON 후보를 불변 내부 값으로 변환하며 실패를 빈 제안으로 바꾸지 않는다."""

    if schema_version in {
        "refresh-plan-v1",
        "refresh-candidate-v7",
        "refresh-candidate-v8",
    }:
        from app.adapters.refresh_workflow_schema import parse_workflow

        if model_input is None:
            raise RefreshFailure(
                Failure("MODEL_INVALID_RESPONSE", "Workflow context required"),
                usage=usage,
            )
        return parse_workflow(text, usage, model_input)

    def fail(message, diagnostic=None):
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", message),
            diagnostics=(diagnostic,) if diagnostic is not None else (),
            usage=usage,
            candidate_text=text,
        ) from None

    try:
        if schema_version == "refresh-candidate-v1":
            dto = LegacyCandidateDTO.model_validate_json(text)
        elif schema_version == "refresh-candidate-v2":
            dto = CandidateDTO.model_validate_json(text)
        elif schema_version == "refresh-candidate-v3":
            dto = MarkerCandidateDTO.model_validate_json(text)
        elif schema_version == "refresh-candidate-v4":
            dto = ExcerptFactCandidateDTO.model_validate_json(text)
        elif schema_version == CANDIDATE_VERSION:
            dto = FactCandidateDTO.model_validate_json(text)
        elif schema_version == CONFIDENCE_CANDIDATE_VERSION:
            dto = ConfidenceCandidateDTO.model_validate_json(text)
        else:
            raise ValueError("Unsupported candidate schema")
    except ValidationError as error:
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", "Invalid model candidate"),
            diagnostics=schema_diagnostics(error, text),
            usage=usage,
            candidate_text=text,
        ) from None
    except ValueError:
        fail("Invalid model candidate")

    groups = (
        ("document_proposals", dto.document_proposals),
        ("relation_proposals", dto.relation_proposals),
        ("new_document_proposals", getattr(dto, "new_document_proposals", [])),
    )
    proposals = [item for _, group in groups for item in group]
    try:
        sources = (
            context_evidence(model_input)
            if schema_version in MARKER_VERSIONS
            and (proposals or getattr(dto, "facts", ()) or model_input is not None)
            else {}
        )
    except (ValidationError, ValueError, TypeError):
        fail("Invalid model candidate evidence")
    if schema_version in MARKER_VERSIONS:
        for name, group in groups:
            for index, item in enumerate(group):
                for source_index, evidence in enumerate(getattr(item, "evidence", ())):
                    if evidence.marker not in sources:
                        fail(
                            "Invalid model candidate evidence",
                            ValidationDiagnostic(
                                f"{name}[{index}].evidence[{source_index}].marker",
                                "source.marker_known",
                                source_marker=evidence.marker,
                                offending_value=evidence.marker,
                            ),
                        )

    facts, fact_refs = {}, {}
    try:
        for index, item in enumerate(getattr(dto, "facts", ())):
            path = f"facts[{index}]"
            if not item.fact_id.strip() or item.fact_id in fact_refs:
                fail(
                    "Invalid structured fact source",
                    ValidationDiagnostic(
                        path + ".fact_id",
                        "fact.identifier_unique",
                        item.fact_id,
                        item.source.marker,
                        diagnostic_value(item.fact_id),
                    ),
                )
            if item.source.marker not in sources:
                fail(
                    "Invalid structured fact source",
                    ValidationDiagnostic(
                        path + ".source.marker",
                        "source.marker_known",
                        item.fact_id,
                        item.source.marker,
                        diagnostic_value(item.source.marker),
                    ),
                )
            source = sources[item.source.marker]
            if schema_version == "refresh-candidate-v4":
                excerpt = item.source.excerpt
                if (
                    not excerpt.strip()
                    or len(excerpt) > 500
                    or source.quote.find(excerpt) < 0
                    or source.quote.find(excerpt, source.quote.find(excerpt) + 1) >= 0
                ):
                    fail(
                        "Invalid structured fact source",
                        ValidationDiagnostic(
                            path + ".source.excerpt",
                            "source.excerpt_exact",
                            item.fact_id,
                            item.source.marker,
                            diagnostic_value(excerpt),
                        ),
                    )
                start = source.start + source.quote.index(excerpt)
                source = Evidence(
                    source.document_id,
                    source.revision_no,
                    start,
                    start + len(excerpt),
                    excerpt,
                )
            values = item.model_dump(exclude={"source", "fact_id"})
            for role in ("actor", "recipient", "object"):
                if values[role] is not None:
                    values[role] = FactEntity(**values[role])
            fact = SourceFact(
                "",
                **values,
                evidence=(source,),
            )
            value = asdict(fact)
            value.pop("fact_id")
            value.pop("confidence")
            canonical_id = (
                "F"
                + sha256(
                    json.dumps(value, ensure_ascii=False, sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest()
            )
            fact = replace(fact, fact_id=canonical_id)
            facts[canonical_id] = fact
            fact_refs[item.fact_id] = canonical_id
    except (KeyError, ValueError, TypeError):
        fail("Invalid structured fact source")

    def values(item, path) -> dict:
        data = item.model_dump(exclude={"evidence"})
        if "fact_ids" in data:
            for index, key in enumerate(item.fact_ids):
                if key not in fact_refs:
                    fail(
                        "Unknown declared fact reference",
                        ValidationDiagnostic(
                            f"{path}.fact_ids[{index}]",
                            "fact.reference_known",
                            key,
                            offending_value=diagnostic_value(key),
                        ),
                    )
            if len(item.fact_ids) != len(set(item.fact_ids)):
                key = next(
                    key
                    for index, key in enumerate(item.fact_ids)
                    if key in item.fact_ids[:index]
                )
                fail(
                    "Duplicate fact reference",
                    ValidationDiagnostic(
                        path + ".fact_ids",
                        "fact.reference_unique",
                        key,
                        offending_value=diagnostic_value(key),
                    ),
                )
            data["fact_ids"] = tuple(
                dict.fromkeys(fact_refs[key] for key in item.fact_ids)
            )
        data["evidence"] = tuple(
            sources[e.marker]
            if schema_version in MARKER_VERSIONS
            else Evidence(**e.model_dump())
            for e in getattr(item, "evidence", ())
        )
        if isinstance(item, FactRelationDTO):
            referenced = [facts[key] for key in data["fact_ids"] if key in facts]
            data["description"] = fact_description(referenced)
            data["evidence"] = unique_evidence(
                e for fact in referenced for e in fact.evidence
            )
        return data

    return ModelCandidate(
        tuple(
            DocumentProposal(**values(item, f"document_proposals[{index}]"))
            for index, item in enumerate(dto.document_proposals)
        ),
        usage,
        tuple(
            RelationProposal(**values(item, f"relation_proposals[{index}]"))
            for index, item in enumerate(dto.relation_proposals)
        ),
        tuple(
            NewDocumentProposal(**values(item, f"new_document_proposals[{index}]"))
            for index, item in enumerate(getattr(dto, "new_document_proposals", ()))
        ),
        tuple(sorted(facts.values(), key=lambda fact: fact.fact_id)),
    )
