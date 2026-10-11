"""개별 사실의 출처·명시 식별·제안 참조 검사. 역할의 의미적 정답은 보증하지 않는다."""

from unicodedata import normalize

from app.refresh.evidence import make_chunks, unique_evidence
from app.refresh.models import DocumentState, FactEntity, SourceFact
from app.refresh.selection import reject


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
):
    """참조가 원문의 명시 표현을 넘지 않게 제한한다. 함의 검사는 별도 평가가 맡는다."""
    limit = 100 * snapshot.request.settings.max_model_calls if aggregate else 100
    if not isinstance(candidate.facts, tuple) or len(candidate.facts) > limit:
        reject("INVALID_FACT", "Structured fact budget exceeded")
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
    for fact in candidate.facts:
        if (
            not isinstance(fact, SourceFact)
            or not bounded(fact.fact_id, 200)
            or fact.fact_id in facts
            or not bounded(fact.action, 200)
            or not bounded(fact.statement, 500)
            or not all(
                isinstance(getattr(fact, key), str)
                for key in ("certainty", "extent", "time")
            )
            or fact.certainty not in {"CONFIRMED", "REPORTED", "PLANNED", "UNKNOWN"}
            or fact.extent not in {"PARTIAL", "COMPLETE", "UNSPECIFIED"}
            or fact.time not in {"CURRENT", "PAST", "FUTURE", "UNSPECIFIED"}
        ):
            reject("INVALID_FACT", "Bounded fact with explicit status required")
        evidence = validate_evidence(snapshot, fact.evidence)
        if len(evidence) != 1:
            reject("INVALID_FACT", "One original fact source required")
        if schema_version == "refresh-candidate-v5":
            if (
                evidence[0] not in chunks
                or len(evidence[0].quote) > snapshot.request.settings.chunk_chars
            ):
                reject("INVALID_FACT", "Fact source must match one server chunk")
        elif len(evidence[0].quote) > 500:
            reject("INVALID_FACT", "Legacy fact excerpt exceeds its budget")
        excerpt = evidence[0].quote
        if fact.action not in excerpt:
            reject("INVALID_FACT", "Fact action must preserve source wording")
        for role in ("actor", "recipient", "object"):
            entity = getattr(fact, role)
            if entity is None:
                continue
            if (
                not isinstance(entity, FactEntity)
                or not bounded(entity.surface, 200)
                or entity.surface not in excerpt
                or entity.surface not in fact.statement
                or (
                    entity.document_id is not None
                    and (
                        not isinstance(entity.document_id, str)
                        or entity.document_id not in names
                    )
                )
            ):
                reject("INVALID_FACT", "Roles must preserve their source surfaces")
            if (
                entity.document_id is not None
                and role in {"actor", "recipient"}
                and identity(entity.surface) not in names[entity.document_id]
            ):
                reject(
                    "INVALID_FACT", "Actor identity requires an explicit name or alias"
                )
        facts[fact.fact_id] = fact
    proposals = (
        candidate.document_proposals
        + candidate.new_document_proposals
        + candidate.relation_proposals
    )
    for proposal in proposals:
        if not hasattr(proposal, "fact_ids"):
            reject("INVALID_MODEL_RESPONSE", "Typed fact-bearing proposal required")
        refs = proposal.fact_ids
        if (
            not isinstance(refs, tuple)
            or (required and not refs)
            or not all(isinstance(key, str) and key in facts for key in refs)
            or len(refs) != len(set(refs))
        ):
            reject(
                "INVALID_FACT_REFERENCE",
                "Proposals require known unique fact references",
            )
        if refs and proposal in candidate.relation_proposals:
            selected = [facts[key] for key in refs]
            if proposal.description != fact_description(selected) or unique_evidence(
                proposal.evidence
            ) != unique_evidence(e for fact in selected for e in fact.evidence):
                reject(
                    "INVALID_RELATION_FACT",
                    "Relation must use exactly its referenced facts",
                )
        elif refs:
            # A source chunk may contain the narrower fact excerpt. Each fact must
            # still belong to the document proposal's declared source coverage.
            if any(
                not any(
                    broad.document_id == narrow.document_id
                    and broad.revision_no == narrow.revision_no
                    and broad.start <= narrow.start < narrow.end <= broad.end
                    for broad in proposal.evidence
                )
                for key in refs
                for narrow in facts[key].evidence
            ):
                reject(
                    "INVALID_FACT_REFERENCE", "Fact is outside proposal source coverage"
                )
    # Unused intermediate facts do not constitute proposals. Validate them above,
    # then let result normalization retain only facts referenced by final proposals.
    return facts
