"""개별 사실의 출처·문서/제안 참조 검사. 역할의 의미적 정답은 보증하지 않는다."""

from dataclasses import replace
from math import isfinite

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import make_chunks, unique_evidence
from app.refresh.models import (
    DocumentState,
    FactEntity,
    Failure,
    SourceFact,
    ValidationDiagnostic,
)
from app.refresh.tolerance import resolve_id


def bounded(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def valid_confidence(value):
    """점수의 형식만 검사한다. 신뢰도 임계값으로 사실을 거절하지 않는다."""
    return type(value) in {int, float} and isfinite(value) and 0 <= value <= 1


def fact_description(facts):
    """같은 사실 문장은 중복 없이 보존한다. 원문 evidence는 별도로 유지한다."""
    return "\n".join(dict.fromkeys(fact.statement for fact in facts))


def validate_facts(
    snapshot,
    candidate,
    validate_evidence,
    *,
    required,
    aggregate=False,
    schema_version=None,
    source_markers=None,
):
    """출처 마커와 알려진 문서·제안 참조를 검사한다. 함의 검사는 별도 평가가 맡는다."""

    diagnostics = []
    first_failure = None

    def fail(
        path,
        rule,
        message,
        *,
        fact=None,
        value=None,
        code="INVALID_FACT",
        collect=False,
    ):
        nonlocal first_failure
        fact_id = getattr(fact, "fact_id", None)
        marker = None
        evidence = getattr(fact, "evidence", ())
        if source_markers and isinstance(evidence, tuple):
            for item in evidence:
                try:
                    marker = source_markers.get(item)
                except TypeError:
                    continue
                if marker is not None:
                    break
        if first_failure is None:
            first_failure = Failure(code, message)
        diagnostics.append(
            ValidationDiagnostic(
                path,
                rule,
                fact_id if isinstance(fact_id, str) else None,
                marker,
                str(value)[:200]
                if isinstance(value, (str, int, float, bool))
                else None,
            )
        )
        if not collect:
            raise RefreshFailure(
                first_failure, diagnostics=tuple(diagnostics)
            ) from None

    limit = 100 * snapshot.request.settings.max_model_calls if aggregate else 100
    if not isinstance(candidate.facts, tuple) or len(candidate.facts) > limit:
        fail("facts", "fact.count_bounded", "Structured fact budget exceeded")
    documents = {
        d.document_id: d
        for d in snapshot.documents
        if d.state == DocumentState.ACTIVE
        and d.project_id == snapshot.request.job.project_id
    }
    # 묶음 실행에서 모델에게 보여 준 묶음 밖 설정도 사실의 등장 문서로 참조할 수 있다.
    known_ids = (
        set(documents)
        | {p.candidate_id for p in candidate.new_document_proposals}
        | {d.document_id for d in getattr(snapshot, "known_settings", ())}
    )
    facts = {}
    chunks = (
        {chunk.evidence for chunk in make_chunks(snapshot)}
        if schema_version
        in {
            "refresh-candidate-v5",
            "refresh-candidate-v6",
            "refresh-candidate-v7",
            "refresh-candidate-v8",
        }
        else set()
    )
    for index, fact in enumerate(candidate.facts):
        path = f"facts[{index}]"
        if not isinstance(fact, SourceFact):
            fail(path, "fact.typed", "Bounded fact with explicit status required")
        if (
            schema_version in {"refresh-candidate-v6", "refresh-candidate-v8"}
            or fact.confidence is not None
        ) and not valid_confidence(fact.confidence):
            fail(
                path + ".confidence",
                "fact.confidence_bounded",
                "Confidence must be a finite number between zero and one",
                fact=fact,
                value=fact.confidence,
            )
        for field, maximum in (("fact_id", 200), ("action", 200), ("statement", 500)):
            value = getattr(fact, field)
            if not bounded(value, maximum):
                fail(
                    path + "." + field,
                    f"fact.{field}_bounded",
                    "Bounded fact with explicit status required",
                    fact=fact,
                    value=value,
                )
        if fact.fact_id in facts:
            fail(
                path + ".fact_id",
                "fact.identifier_unique",
                "Bounded fact with explicit status required",
                fact=fact,
                value=fact.fact_id,
            )
        for field, allowed in (
            ("certainty", {"CONFIRMED", "REPORTED", "PLANNED", "UNKNOWN"}),
            ("extent", {"PARTIAL", "COMPLETE", "UNSPECIFIED"}),
            ("time", {"CURRENT", "PAST", "FUTURE", "UNSPECIFIED"}),
        ):
            value = getattr(fact, field)
            if not isinstance(value, str) or value not in allowed:
                fail(
                    path + "." + field,
                    f"fact.{field}_known",
                    "Bounded fact with explicit status required",
                    fact=fact,
                    value=value,
                )
        try:
            evidence = validate_evidence(snapshot, fact.evidence)
        except RefreshFailure as error:
            fail(
                path + ".source",
                "source.original_matches",
                error.failure.message,
                fact=fact,
                code=error.failure.code,
            )
        if len(evidence) != 1:
            fail(
                path + ".source",
                "source.single_marker",
                "One original fact source required",
                fact=fact,
            )
        if schema_version in {
            "refresh-candidate-v5",
            "refresh-candidate-v6",
            "refresh-candidate-v7",
            "refresh-candidate-v8",
        }:
            if (
                evidence[0] not in chunks
                or len(evidence[0].quote) > snapshot.request.settings.chunk_chars
            ):
                fail(
                    path + ".source.marker",
                    "source.server_chunk",
                    "Fact source must match one server chunk",
                    fact=fact,
                )
        elif len(evidence[0].quote) > 500:
            fail(
                path + ".source.excerpt",
                "source.excerpt_bounded",
                "Legacy fact excerpt exceeds its budget",
                fact=fact,
            )
        for role in ("actor", "recipient", "object"):
            entity = getattr(fact, role)
            if entity is None:
                continue
            role_path = path + "." + role
            if not isinstance(entity, FactEntity):
                fail(
                    role_path,
                    "fact.role_typed",
                    "Roles must preserve their source surfaces",
                    fact=fact,
                )
            if not bounded(entity.surface, 200):
                fail(
                    role_path + ".surface",
                    "fact.role_surface_bounded",
                    "Roles must preserve their source surfaces",
                    fact=fact,
                    value=entity.surface,
                )
            if entity.document_id is not None and (
                not isinstance(entity.document_id, str)
                or entity.document_id not in known_ids
            ):
                resolved = resolve_id(entity.document_id, known_ids)
                if resolved is None:
                    fail(
                        role_path + ".document_id",
                        "fact.entity_reference_known",
                        "Roles must preserve their source surfaces",
                        fact=fact,
                        value=entity.document_id,
                        collect=True,
                    )
                else:
                    # 옮겨 적다 몇 글자 틀린 ID는 가장 가까운 실제 ID로 고친다.
                    fact = replace(
                        fact, **{role: replace(entity, document_id=resolved)}
                    )
            # 초기 구현에서는 surface와 문서 제목·이름·별칭의 일치를 차단 조건으로
            # 사용하지 않는다. 문서 ID의 존재·프로젝트 범위만 검사한다.
        facts[fact.fact_id] = fact
    if first_failure is not None:
        raise RefreshFailure(first_failure, diagnostics=tuple(diagnostics)) from None
    proposals = tuple(
        (f"{group}[{index}]", proposal)
        for group, values in (
            ("document_proposals", candidate.document_proposals),
            ("new_document_proposals", candidate.new_document_proposals),
            ("relation_proposals", candidate.relation_proposals),
        )
        for index, proposal in enumerate(values)
    )
    for path, proposal in proposals:
        if not hasattr(proposal, "fact_ids"):
            fail(
                path + ".fact_ids",
                "fact.references_typed",
                "Typed fact-bearing proposal required",
                code="INVALID_MODEL_RESPONSE",
            )
        refs = proposal.fact_ids
        if not isinstance(refs, tuple) or (required and not refs):
            fail(
                path + ".fact_ids",
                "fact.references_nonempty"
                if required and not refs
                else "fact.references_tuple",
                "Proposals require known unique fact references",
                code="INVALID_FACT_REFERENCE",
            )
        for index, key in enumerate(refs):
            if not isinstance(key, str) or key not in facts:
                fail(
                    f"{path}.fact_ids[{index}]",
                    "fact.reference_known",
                    "Proposals require known unique fact references",
                    value=key,
                    code="INVALID_FACT_REFERENCE",
                )
            if key in refs[:index]:
                fail(
                    f"{path}.fact_ids[{index}]",
                    "fact.reference_unique",
                    "Proposals require known unique fact references",
                    fact=facts[key],
                    value=key,
                    code="INVALID_FACT_REFERENCE",
                )
        if refs and proposal in candidate.relation_proposals:
            selected = [facts[key] for key in refs]
            if proposal.description != fact_description(selected) or unique_evidence(
                proposal.evidence
            ) != unique_evidence(e for fact in selected for e in fact.evidence):
                fail(
                    path + ".fact_ids",
                    "relation.facts_exact",
                    "Relation must use exactly its referenced facts",
                    fact=selected[0],
                    code="INVALID_RELATION_FACT",
                )
        elif refs:
            # A source chunk may contain the narrower fact excerpt. Each fact must
            # still belong to the document proposal's declared source coverage.
            for index, key in enumerate(refs):
                if any(
                    not any(
                        broad.document_id == narrow.document_id
                        and broad.revision_no == narrow.revision_no
                        and broad.start <= narrow.start < narrow.end <= broad.end
                        for broad in proposal.evidence
                    )
                    for narrow in facts[key].evidence
                ):
                    fail(
                        f"{path}.fact_ids[{index}]",
                        "fact.reference_source_coverage",
                        "Fact is outside proposal source coverage",
                        fact=facts[key],
                        value=key,
                        code="INVALID_FACT_REFERENCE",
                    )
    # Unused intermediate facts do not constitute proposals. Validate them above,
    # then let result normalization retain only facts referenced by final proposals.
    return facts
