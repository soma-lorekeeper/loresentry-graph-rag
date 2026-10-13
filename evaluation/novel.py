"""소설 회차 JSON을 S3 입력 대체 값으로 읽는 평가 조립. 운영 전송 계약은 아니다."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.adapters.llm_schema import parse_candidate
from app.adapters.refresh_schema import omit_absent_titles
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
    RelationProposal,
    Usage,
)
from app.refresh.policy import RELATION_KEYS
from evaluation.cases import EvaluationCase
from evaluation.semantics import BodyCriterion, ProposalScope

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "docs/examples/refresh/novel"
SETTINGS_ROOT = (
    Path(__file__).resolve().parents[1]
    / "docs/examples/novel/the-thirteenth-bell/settings"
)


class SettingFile(BaseModel):
    """Markdown 원본을 Content 조회 fake의 문서 메타데이터와 연결한다."""

    model_config = ConfigDict(strict=True, extra="forbid")
    document_id: str
    revision_no: int
    folder_code: Literal[
        "CHARACTER", "ITEM", "WORLDVIEW", "LOCATION", "ORGANIZATION", "EVENT"
    ]
    name: str
    file: str
    alias: str | None = None


class SettingsManifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    documents: tuple[SettingFile, ...]


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
    source_value, context_value = (
        source.model_dump(mode="json"),
        context.model_dump(mode="json"),
    )
    omit_absent_titles(source_value["documents"])
    omit_absent_titles(context_value["documents"])
    canonical = json.dumps(
        [source_value, context_value],
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
    expected = parse_candidate(
        (directory / "expected.json").read_text(),
        Usage(1),
        schema_version="refresh-candidate-v1",
    )
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


def novel_settings_case(
    model: str, trial: str, settings_directory: Path = SETTINGS_ROOT
) -> EvaluationCase:
    """수정 전 5화 기준 설정 파일과 변경 원고를 함께 평가한다. 과거 fixture는 유지한다."""
    base = novel_case(model, trial)
    manifest = SettingsManifest.model_validate_json(
        (settings_directory / "manifest.json").read_text(encoding="utf-8")
    )
    documents = []
    for item in manifest.documents:
        if Path(item.file).name != item.file or not item.file.endswith(".md"):
            raise ValueError("Setting file must be a local Markdown filename")
        text = (settings_directory / item.file).read_text(encoding="utf-8")
        title, separator, body = text.partition("\n\n")
        if not separator or title != f"# {item.name}" or not body.strip():
            raise ValueError("Setting title or body differs from manifest")
        documents.append(
            DocumentSnapshot(
                base.request.job.project_id,
                item.document_id,
                item.revision_no,
                body.removesuffix("\n"),
                properties=(("name", item.name),),
                folder_code=item.folder_code,
                title=item.name,
            )
        )
        if item.alias is not None:
            documents[-1] = replace(
                documents[-1],
                properties=documents[-1].properties + (("alias", item.alias),),
            )
    if not documents or len({d.document_id for d in documents}) != len(documents):
        raise ValueError("Settings must have nonempty, unique document IDs")
    canonical = json.dumps(
        [
            NovelInput(
                schema_version="evaluation-input-v1",
                project_id=base.request.job.project_id,
                documents=base.source.documents,
            ).model_dump(mode="json"),
            NovelContext(
                documents=tuple(documents),
                relations=base.related.relations,
                observed_version="novel-settings-before-05r2-06r1",
            ).model_dump(mode="json"),
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    job = replace(
        base.request.job,
        request_id=f"{trial}-novel-settings",
        input_fingerprint=sha256(canonical.encode()).hexdigest(),
    )
    expected = parse_candidate(
        (FIXTURE_ROOT / "expected-with-settings.json").read_text(encoding="utf-8"),
        Usage(1),
        schema_version="refresh-candidate-v2",
    )
    allowed_new = (
        ("LOCATION", "강연오의 임시 창고"),
        ("LOCATION", "강연오의 창고"),
        ("LOCATION", "강연오의 임시 거주지"),
        ("LOCATION", "기억 반환소"),
        ("ITEM", "봉인 유리통"),
        ("ITEM", "기억수 유리통"),
        ("EVENT", "강연오의 기억 반환"),
        ("EVENT", "강연오 기억 반환"),
    )
    return replace(
        base,
        name="novel-settings",
        request=replace(
            base.request,
            job=job,
            target_ids=tuple(d.document_id for d in documents),
            settings=replace(base.request.settings, separate_discovery=True),
        ),
        source=replace(base.source, job=job),
        targets=tuple(documents),
        related=replace(
            base.related,
            document_ids=tuple(d.document_id for d in documents),
            observed_version="novel-settings-before-05r2-06r1",
        ),
        expected=expected,
        required_words=(
            ("character-doyun", ("강철", "방문", "오후", "한 달")),
            ("character-haeju", ("도윤", "봉인", "승인")),
            ("item-finger", ("강철", "나사축")),
        ),
        preserved_words=(
            ("character-doyun", ("배달부 조합", "윤준")),
            ("character-haeju", ("회색 머리", "화상 자국")),
            ("item-finger", ("교체", "중앙 기록청")),
            ("character-yunjun", ("한 달", "잃어버린 기억")),
            ("worldview-memory-tax", ("중복 납부", "소진", "다시 결정")),
            ("organization-record-office", ("직무에서 물러", "조사", "청구")),
        ),
        allowed_document_targets=tuple(d.document_id for d in documents),
        allowed_new_settings=allowed_new,
        forbidden_new_settings=(
            ("LOCATION", "항만 지하 비밀시설"),
            ("LOCATION", "항만의 비밀 시설"),
            ("ITEM", "항만 지하의 종"),
        ),
        new_required_words=(("CHARACTER", "강연오", ("등대", "신청", "노래")),),
        # Affirmative body criteria below distinguish completion from a negated
        # or historical mention of the same words.
        new_forbidden_words=(),
        allowed_relations=novel_allowed_relations(
            base.source.documents + tuple(documents), allowed_new
        ),
        body_criteria=(
            BodyCriterion(
                "도윤 파견 기간의 본문 표현",
                ProposalScope("document", "character-doyun"),
                required_patterns=(
                    r"(?:기한|파견|기간)[^.!?。]{0,20}한 달",
                    r"한 달[^.!?。]{0,20}(?:파견|담당)",
                ),
                forbidden_patterns=(
                    r"(?:백)?도윤(?:은|이|가) (?:두 달|세 달|무기한) 동안 임시 파견(?:되어|돼) [^.!?。]{0,30}(?:맡는다|담당한다|근무한다)(?=[.!?。]|$)",
                    r"(?:파견 기간|파견 기한)(?:은|이)? (?:두 달|세 달|무기한)(?:이다|이었다)(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "도윤 이름표 소유자의 오귀속 금지",
                ProposalScope("document", "character-doyun"),
                forbidden_patterns=(
                    r"(?:조합장|반장)(?:의|이 가진) (?:이름표|명찰)(?: 뒤|에|에는)[^.!?。]{0,20}임시 파견[^.!?。]{0,20}(?:붙어 있었다|적혀 있었다|기록되어 있었다|있었다)(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "도윤에게 승인과 봉인 해제를 이전하지 않음",
                ProposalScope("document", "character-doyun"),
                forbidden_patterns=(
                    r"(?:백)?도윤(?:은|이|가) (?:반환 승인(?:과 봉인 해제)?|봉인 해제)(?:를|을)? (?:담당한다|맡는다|책임진다)(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "서명과 현재 동의를 구분하는 본문 표현",
                ProposalScope("document", "character-haeju"),
                required_patterns=(
                    r"(?:지금|현재)[^.!?。]{0,15}동의",
                    r"서명[^.!?。]{0,30}동의[^.!?。]{0,20}(?:대신할 수 없|별개|구분)",
                ),
                forbidden_patterns=(
                    r"서명(?:이|은|만으로)[^.!?。]{0,25}(?:현재|지금)의? 동의(?:를|를 충분히) 대신한다(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "강연오의 부분 반환 본문 표현",
                ProposalScope("new", "new:CHARACTER:강연오"),
                required_patterns=(
                    r"노래[^.!?。]{0,15}(?:한 소절|일부)",
                    r"(?:한 소절|일부)[^.!?。]{0,15}노래",
                ),
                forbidden_patterns=(
                    r"(?:모든 기억|기억 전부|기억 전체|기억을 완전히)[^.!?。]{0,20}(?:회복했다|되찾았다|돌려받았다)(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "강연오가 정확한 가사를 되찾지 못한 상태",
                ProposalScope("new", "new:CHARACTER:강연오"),
                required_patterns=(
                    r"가사[^.!?。]{0,30}(?:못|않|기억나지)",
                    r"(?:못|않)[^.!?。]{0,15}가사",
                ),
                forbidden_patterns=(
                    r"정확한 가사(?:를|가)[^.!?。]{0,12}(?:기억한다|기억했다|되찾았다)(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "강연오 딸의 귀환을 새 사실로 만들지 않음",
                ProposalScope("new", "new:CHARACTER:강연오"),
                forbidden_patterns=(
                    r"딸(?:은|이) 살아 (?:돌아왔다|있다)(?=[.!?。]|$)",
                    r"딸(?:은|이) 다시 살아났다(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "기억 반환 이벤트의 전체 회복 단정 금지",
                ProposalScope("new", "new:EVENT:강연오의 기억 반환"),
                forbidden_patterns=(
                    r"(?:모든 기억|기억 전부|기억 전체|기억을 완전히)[^.!?。]{0,20}(?:회복했다|되찾았다|돌려받았다)(?=[.!?。]|$)",
                    r"기억(?:을|이|은)? 완전히 회복(?:했다|되었다)(?=[.!?。]|$)",
                ),
            ),
            BodyCriterion(
                "기억세 징수 중지를 완전 폐지로 단정하지 않음",
                ProposalScope("document", "worldview-memory-tax"),
                forbidden_patterns=(
                    r"기억세(?:는|가)? 완전히 폐지(?:됐다|되었다)(?=[.!?。]|$)",
                ),
            ),
        ),
    )


def novel_allowed_relations(documents, allowed_new):
    """원고에서 확인한 문서쌍의 추가 연결을 허용한다. 설명의 의미를 보증하지 않는다."""
    metadata = {d.document_id: (d.folder_code, d.revision_no) for d in documents}
    metadata["new:CHARACTER:강연오"] = ("CHARACTER", None)
    pairs = {
        ("character-doyun", "character-seoha"),
        ("character-seoha", "item-finger"),
        ("character-seoha", "organization-record-office"),
        ("character-haeju", "worldview-memory-tax"),
        ("chapter-06", "character-seoha"),
    }
    actors = ("character-doyun", "character-haeju", "character-seoha")
    for actor in actors:
        pairs.add((actor, "new:CHARACTER:강연오"))
    for code, name in allowed_new:
        target = f"new:{code}:{name}"
        metadata[target] = (code, None)
        for source in ("chapter-06", "new:CHARACTER:강연오", *actors):
            pairs.add((source, target))
    return tuple(
        RelationProposal(
            source,
            target,
            metadata[source][1],
            metadata[target][1],
            RELATION_KEYS[metadata[target][0]],
            RELATION_KEYS[metadata[source][0]],
            "평가용 허용 문서쌍",  # 비교 기준이며 모델에는 전달하지 않는다.
            (),
        )
        for source, target in sorted(pairs)
    )
