"""개별 사실의 출처·명시 식별·제안 참조 검사. 역할의 의미적 정답은 보증하지 않는다."""

from unicodedata import normalize

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import make_chunks, unique_evidence
from app.refresh.models import (
    DocumentState,
    FactEntity,
    Failure,
    SourceFact,
    ValidationDiagnostic,
)


def identity(value):
    return " ".join(normalize("NFKC", value).split()).casefold()


def bounded(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


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
    """참조가 원문의 명시 표현을 넘지 않게 제한한다. 함의 검사는 별도 평가가 맡는다."""

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
    names = {
        key: {
            identity(value)
            for value in [d.title]
            + [v for k, v in d.properties if k in {"name", "title", "alias"}]
            if isinstance(value, str) and value.strip()
        }
        for key, d in documents.items()
    }
    names.update(
        {p.candidate_id: {identity(p.name)} for p in candidate.new_document_proposals}
    )
    facts = {}
    chunks = (
        {chunk.evidence for chunk in make_chunks(snapshot)}
        if schema_version == "refresh-candidate-v5"
        else set()
    )
    for index, fact in enumerate(candidate.facts):
        path = f"facts[{index}]"
        if not isinstance(fact, SourceFact):
            fail(path, "fact.typed", "Bounded fact with explicit status required")
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
        if schema_version == "refresh-candidate-v5":
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
        excerpt = evidence[0].quote
        # With typed fields and a verified source, these leaf checks are independent.
        # Keep the first failure while reporting every safely checkable fact/role.
        if fact.action not in excerpt:
            fail(
                path + ".action",
                "fact.action_in_source",
                "Fact action must preserve source wording",
                fact=fact,
                value=fact.action,
                collect=True,
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
            if entity.surface not in excerpt:
                fail(
                    role_path + ".surface",
                    "fact.role_surface_in_source",
                    "Roles must preserve their source surfaces",
                    fact=fact,
                    value=entity.surface,
                    collect=True,
                )
            if entity.surface not in fact.statement:
                fail(
                    role_path + ".surface",
                    "fact.role_surface_in_statement",
                    "Roles must preserve their source surfaces",
                    fact=fact,
                    value=entity.surface,
                    collect=True,
                )
            if entity.document_id is not None and (
                not isinstance(entity.document_id, str)
                or entity.document_id not in names
            ):
                fail(
                    role_path + ".document_id",
                    "fact.entity_reference_known",
                    "Roles must preserve their source surfaces",
                    fact=fact,
                    value=entity.document_id,
                    collect=True,
                )
            if (
                isinstance(entity.document_id, str)
                and entity.document_id in names
                and role in {"actor", "recipient"}
                and identity(entity.surface) not in names[entity.document_id]
            ):
                fail(
                    role_path + ".document_id",
                    "fact.entity_identity_matches",
                    "Actor identity requires an explicit name or alias",
                    fact=fact,
                    value=entity.document_id,
                    collect=True,
                )
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
