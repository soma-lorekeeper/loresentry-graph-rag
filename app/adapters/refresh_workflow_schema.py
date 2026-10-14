"""2단계 작업 계획·문단 작성 응답의 엄격한 전송 계약."""

from typing import Literal

from pydantic import ValidationError

from app.adapters.llm_schema import (
    ConfidenceSourceFactDTO,
    FactNewDocumentDTO,
    FactRelationDTO,
    MarkerSourceFactDTO,
    StrictDTO,
    schema_diagnostics,
)
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    ChangePlan,
    Failure,
    PlannedEdit,
    PlannedNewDocument,
    PlannedRelation,
)
from app.refresh.workflow import PLAN_SCHEMA, materialize_candidate


class EditPlanDTO(StrictDTO):
    edit_id: str
    document_id: str
    base_revision_no: int
    paragraph_id: str
    operation: Literal["REPLACE", "DELETE", "APPEND"]
    instruction: str
    source_markers: list[str]


class NewPlanDTO(StrictDTO):
    candidate_id: str
    folder_code: Literal[
        "WORLDVIEW", "CHARACTER", "LOCATION", "ORGANIZATION", "ITEM", "EVENT"
    ]
    name: str
    instruction: str
    source_markers: list[str]


class RelationPlanDTO(StrictDTO):
    document_id: str
    target_document_id: str
    source_markers: list[str]


class PlanDTO(StrictDTO):
    reviewed_document_ids: list[str]
    edits: list[EditPlanDTO]
    new_documents: list[NewPlanDTO]
    relations: list[RelationPlanDTO]


class ParagraphEditDTO(StrictDTO):
    edit_id: str
    text: str
    fact_ids: list[str]


class WrittenCandidateDTO(StrictDTO):
    facts: list[MarkerSourceFactDTO]
    paragraph_edits: list[ParagraphEditDTO]
    new_document_proposals: list[FactNewDocumentDTO]
    relation_proposals: list[FactRelationDTO]


class ConfidenceWrittenCandidateDTO(WrittenCandidateDTO):
    facts: list[ConfidenceSourceFactDTO]


def response_dto(version):
    return {
        PLAN_SCHEMA: PlanDTO,
        "refresh-candidate-v7": WrittenCandidateDTO,
        "refresh-candidate-v8": ConfidenceWrittenCandidateDTO,
    }[version]


def parse_workflow(text, usage, model_input):
    try:
        dto = response_dto(model_input.schema_version).model_validate_json(text)
        if model_input.schema_version != PLAN_SCHEMA:
            return materialize_candidate(dto, usage, model_input)
        return ChangePlan(
            tuple(dto.reviewed_document_ids),
            tuple(
                PlannedEdit(
                    **{
                        **item.model_dump(),
                        "source_markers": tuple(item.source_markers),
                    }
                )
                for item in dto.edits
            ),
            tuple(
                PlannedNewDocument(
                    **{
                        **item.model_dump(),
                        "source_markers": tuple(item.source_markers),
                    }
                )
                for item in dto.new_documents
            ),
            tuple(
                PlannedRelation(
                    **{
                        **item.model_dump(),
                        "source_markers": tuple(item.source_markers),
                    }
                )
                for item in dto.relations
            ),
            usage,
        )
    except ValidationError as error:
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", "Invalid workflow response"),
            diagnostics=schema_diagnostics(error, text),
            usage=usage,
            candidate_text=text,
        ) from None
    except RefreshFailure as error:
        raise RefreshFailure(
            error.failure,
            diagnostics=error.diagnostics,
            usage=usage,
            candidate_text=text,
        ) from None
    except (ValueError, KeyError, TypeError):
        raise RefreshFailure(
            Failure("MODEL_INVALID_RESPONSE", "Invalid workflow response"),
            usage=usage,
            candidate_text=text,
        ) from None
