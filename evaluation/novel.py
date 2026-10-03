"""소설 회차 JSON을 S3 입력 대체 값으로 읽는 평가 조립. 운영 전송 계약은 아니다."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.adapters.llm_schema import parse_candidate
from app.refresh.models import (
    ArtifactRef,
    ChangedDocumentRef,
    DocumentSnapshot,
    InputSnapshot,
    JobKey,
    ModelSettings,
    RefreshRequest,
    RelatedDocuments,
    Relation,
    Usage,
)
from evaluation.cases import EvaluationCase

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "docs/examples/refresh/novel"


class NovelInput(BaseModel):
    """평가 파일의 프로젝트와 변경 문서. 실제 Content S3 스키마로 사용하지 않는다."""

    model_config = ConfigDict(strict=True, extra="forbid")
    schema_version: Literal["evaluation-input-v1"]
    project_id: str
    documents: tuple[DocumentSnapshot, ...]


class NovelContext(BaseModel):
    """기존 설정 본문과 관계 조회 응답을 별도 보존한다."""

    model_config = ConfigDict(strict=True, extra="forbid")
    documents: tuple[DocumentSnapshot, ...]
    relations: tuple[Relation, ...]
    observed_version: str


def novel_case(
    model: str, trial: str, directory: Path = FIXTURE_ROOT
) -> EvaluationCase:
    """실제 input.json을 읽어 fake 저장소에 주입할 스냅샷과 비교 자료를 만든다."""
    source = NovelInput.model_validate_json((directory / "input.json").read_text())
    context = NovelContext.model_validate_json((directory / "context.json").read_text())
    canonical = json.dumps(
        [source.model_dump(mode="json"), context.model_dump(mode="json")],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    job = JobKey(
        source.project_id,
        f"{trial}-novel-refresh",
        sha256(canonical.encode()).hexdigest(),
    )
    request = RefreshRequest(
        job,
        ArtifactRef("fake-content-input", "novel/refresh-05r2-06r1/input.json"),
        tuple(d.document_id for d in source.documents),
        model_settings=ModelSettings(model=model),
        changed_documents=tuple(
            ChangedDocumentRef(d.document_id, d.revision_no, d.state)
            for d in source.documents
        ),
    )
    expected = parse_candidate((directory / "expected.json").read_text(), Usage(1))
    return EvaluationCase(
        "novel-refresh",
        request,
        InputSnapshot(job, source.documents),
        RelatedDocuments(
            tuple(d.document_id for d in context.documents),
            context.relations,
            observed_version=context.observed_version,
        ),
        context.documents,
        expected,
        required_words=(
            ("character-doyun", ("검게 산화시킨 강철", "방문 전달", "민해주")),
            ("character-haeju", ("백도윤", "봉인 해제", "승인")),
            ("item-finger", ("검게 산화시킨 강철", "강철 나사축")),
        ),
        forbidden_words=(("character-doyun", ("황동",)), ("item-finger", ("황동",))),
    )
