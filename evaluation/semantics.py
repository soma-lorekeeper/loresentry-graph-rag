"""구조화 사실 기준 비교. 자유 본문과 원문의 의미적 함의는 사람 판정을 남긴다."""

import re
from dataclasses import dataclass
from unicodedata import normalize


def normalized_text(value: str) -> str:
    return " ".join(normalize("NFKC", value).split()).casefold()


def recognized_action(value: str, aliases: tuple[str, ...]) -> bool:
    """완결 서술어 앞의 목적어 확장은 허용하며, 부정·전언 수식을 추측하지 않는다."""
    actual = normalized_text(value)
    for alias in aliases:
        expected = normalized_text(alias)
        if actual == expected:
            return True
        if actual.endswith(expected):
            prefix = actual[: -len(expected)]
            if (
                not re.search(r"\b(not|never|no|without|cannot)\b|n['’]t\b", prefix)
                and not any(token in prefix.split() for token in ("안", "못"))
                and not any(token in prefix for token in ("않", "아니", "거짓", "허위"))
            ):
                return True
    return False


@dataclass(frozen=True)
class ExpectedEntity:
    """한 역할의 명시적 이름·별칭. 참조는 정확히 일치하며 None은 미해결 역할이다."""

    names: tuple[str, ...]
    document_id: str | None = None

    def __post_init__(self):
        if not self.names or any(not normalized_text(name) for name in self.names):
            raise ValueError("Semantic entity names must be nonempty")


@dataclass(frozen=True)
class ExpectedEvent:
    """사건을 목적어로 표현한 명시적 프레임. 자유 statement에서 역할을 추론하지 않는다.

    이름·객체·사건 명사를 선언한 서식에 맞춰 비교한다. 참여인이 생략된 사건은
    allow_unqualified를 명시한 프레임에서만 일치하며, 그 외에는 미확인이다.
    """

    participant: ExpectedEntity
    object: ExpectedEntity
    actions: tuple[str, ...]
    templates: tuple[str, ...] = ("{participant}의 {object} {action}",)
    allow_unqualified: bool = False

    def __post_init__(self):
        if not self.actions or any(
            not normalized_text(action) for action in self.actions
        ):
            raise ValueError("Semantic event actions must be nonempty")
        if not self.templates or any(
            any(
                template.count("{" + field + "}") != 1
                for field in ("participant", "object", "action")
            )
            or any(
                brace
                in template.replace("{participant}", "")
                .replace("{object}", "")
                .replace("{action}", "")
                for brace in ("{", "}")
            )
            for template in self.templates
        ):
            raise ValueError(
                "Semantic event templates require participant, object and action"
            )


@dataclass(frozen=True)
class SemanticFrame:
    """같은 사건의 대체 역할 배치. action·확실성·범위·시점은 기준과 공유한다."""

    actor: ExpectedEntity | None = None
    recipient: ExpectedEntity | None = None
    object: ExpectedEntity | ExpectedEvent | None = None
    absent_roles: tuple[str, ...] = ()

    def __post_init__(self):
        if len(set(self.absent_roles)) != len(self.absent_roles) or any(
            role not in {"actor", "recipient", "object"}
            or getattr(self, role) is not None
            for role in self.absent_roles
        ):
            raise ValueError("Absent roles must be unique and have no expected entity")


@dataclass(frozen=True)
class ProposalScope:
    """사실이 실제로 참조되어야 할 제안. 관계는 두 끝점의 순서와 무관하다."""

    kind: str = "any"
    document_id: str | None = None
    target_document_id: str | None = None

    def __post_init__(self):
        if self.kind not in {"any", "document", "new", "relation"}:
            raise ValueError("Unknown semantic proposal scope")
        if self.kind != "any" and not self.document_id:
            raise ValueError("Semantic proposal scope requires a document ID")
        if self.kind == "relation" and not self.target_document_id:
            raise ValueError("Semantic relation scope requires both document IDs")


@dataclass(frozen=True)
class SemanticCriterion:
    """필수 또는 금지 사실. 생략한 역할·상태 축은 이 기준에서 평가하지 않는다.

    등록되지 않은 action·표면 표현은 의미 오류로 단정하지 않고 검토 대상으로 남긴다.
    """

    name: str
    actions: tuple[str, ...]
    actor: ExpectedEntity | None = None
    recipient: ExpectedEntity | None = None
    object: ExpectedEntity | None = None
    certainty: str | None = None
    extent: str | None = None
    time: str | None = None
    scope: ProposalScope = ProposalScope()
    forbidden: bool = False
    alternative_frames: tuple[SemanticFrame, ...] = ()

    def __post_init__(self):
        if (
            not self.name.strip()
            or not self.actions
            or any(not normalized_text(action) for action in self.actions)
        ):
            raise ValueError("Semantic criteria require names and action expressions")
        for value, allowed in (
            (self.certainty, {"CONFIRMED", "REPORTED", "PLANNED", "UNKNOWN"}),
            (self.extent, {"PARTIAL", "COMPLETE", "UNSPECIFIED"}),
            (self.time, {"CURRENT", "PAST", "FUTURE", "UNSPECIFIED"}),
        ):
            if value is not None and value not in allowed:
                raise ValueError("Unknown semantic criterion qualifier")


def validate_semantic_criteria(criteria: tuple[SemanticCriterion, ...]):
    if len({criterion.name for criterion in criteria}) != len(criteria):
        raise ValueError("Semantic criterion names must be unique")


@dataclass(frozen=True)
class BodyCriterion:
    """본문에 등록한 좁은 표현 검사. 낯선 의역을 틀렸다고 단정하지 않는다.

    required_patterns는 동등한 표현의 대안이며 forbidden_patterns는 명시적
    오귀속·상태 왜곡이다. 패턴 일치는 전체 본문의 함의나 사실 참조 일치를
    증명하지 않는다. 정규식 작성자는 부정·인용 문맥을 포함해 범위를 제한한다.
    """

    name: str
    scope: ProposalScope
    required_patterns: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.name.strip() or self.scope.kind not in {"document", "new", "any"}:
            raise ValueError("Body criteria require a name and a body proposal scope")
        if not self.required_patterns and not self.forbidden_patterns:
            raise ValueError("Body criteria require explicit expression patterns")
        for pattern in self.required_patterns + self.forbidden_patterns:
            if not pattern.strip():
                raise ValueError("Body criterion patterns must be nonempty")
            re.compile(pattern)


def validate_body_criteria(criteria: tuple[BodyCriterion, ...]):
    if len({criterion.name for criterion in criteria}) != len(criteria):
        raise ValueError("Body criterion names must be unique")


def assess_body_claims(
    criteria,
    result,
    *,
    resolve_expected_id=lambda value: value,
    resolve_actual_id=lambda value: value,
):
    """알려진 오류와 표현 미확인을 분리하며 패턴 통과를 의미 합격으로 부르지 않는다."""
    validate_body_criteria(criteria)
    bodies = [
        ("document", resolve_actual_id(p.target_document_id), p.value)
        for p in result.document_proposals
        if p.field == "body_text"
    ] + [
        ("new", resolve_actual_id(p.candidate_id), p.body_text)
        for p in result.new_document_proposals
    ]
    mismatches, pending, details = [], [], []
    for criterion in criteria:
        scope = criterion.scope
        selected = [
            body
            for kind, document_id, body in bodies
            if scope.kind == "any"
            or (
                kind == scope.kind
                and document_id == resolve_expected_id(scope.document_id)
            )
        ]
        statuses = []
        for body in selected:
            text = normalized_text(body)
            forbidden = [
                pattern
                for pattern in criterion.forbidden_patterns
                if re.search(pattern, text, re.IGNORECASE)
            ]
            matched = [
                pattern
                for pattern in criterion.required_patterns
                if re.search(pattern, text, re.IGNORECASE)
            ]
            if forbidden:
                mismatches.append(f"{criterion.name}: 등록된 본문 오류 표현")
                status = "mismatch"
            elif criterion.required_patterns and not matched:
                pending.append(f"{criterion.name}: 본문 표현 미확인")
                status = "review_required"
            else:
                status = "matched_patterns"
            statuses.append(status)
        details.append(
            {
                "criterion": criterion.name,
                "scope": scope.kind,
                "document_id": scope.document_id,
                "bodies_evaluated": len(selected),
                "statuses": statuses,
                "status": (
                    "not_applicable"
                    if not statuses
                    else "mismatch"
                    if "mismatch" in statuses
                    else "review_required"
                    if "review_required" in statuses
                    else "matched_patterns"
                ),
            }
        )
    return {
        **(
            {
                "body_claim_mismatches": sorted(set(mismatches)),
                "unreviewed_body_claims": sorted(set(pending)),
            }
            if criteria
            else {}
        ),
        "body_assessment": {
            "registered_patterns": (
                "not_configured"
                if not criteria
                else "not_applicable"
                if not any(item["bodies_evaluated"] for item in details)
                else "mismatch"
                if mismatches
                else "review_required"
                if pending
                else "matched_patterns"
            ),
            "criteria_count": len(criteria),
            "details": details,
            "semantic_entailment": (
                "human_review_pending" if bodies else "not_applicable"
            ),
            "fact_alignment": "human_review_pending" if bodies else "not_applicable",
        },
    }


def assess_semantic_claims(
    criteria,
    result,
    *,
    resolve_expected_id=lambda value: value,
    resolve_actual_id=lambda value: value,
):
    """기준 일치·불일치·미평가를 분리하며 fact 존재를 본문 의미 통과로 간주하지 않는다."""
    validate_semantic_criteria(criteria)
    facts = {fact.fact_id: fact for fact in result.facts}
    scopes = {}

    def scope_key(kind, first=None, second=None, resolver=resolve_actual_id):
        if kind == "any":
            return (kind,)
        if kind == "relation":
            return (kind, *sorted((resolver(first), resolver(second))))
        return (kind, resolver(first))

    for proposal in result.document_proposals:
        key = scope_key("document", proposal.target_document_id)
        scopes.setdefault(key, set()).update(proposal.fact_ids)
    for proposal in result.new_document_proposals:
        key = scope_key("new", proposal.candidate_id)
        scopes.setdefault(key, set()).update(proposal.fact_ids)
    for proposal in result.relation_proposals:
        key = scope_key("relation", proposal.document_id, proposal.target_document_id)
        scopes.setdefault(key, set()).update(proposal.fact_ids)
    scopes[("any",)] = set(facts)

    def frames(criterion):
        return (
            SemanticFrame(criterion.actor, criterion.recipient, criterion.object),
            *criterion.alternative_frames,
        )

    def entities(role):
        if isinstance(role, ExpectedEvent):
            return (role.participant, role.object)
        return () if role is None else (role,)

    known_names = {
        normalized_text(name)
        for criterion in criteria
        for frame in frames(criterion)
        for role in (frame.actor, frame.recipient, frame.object)
        for entity in entities(role)
        for name in entity.names
    }

    def event_surfaces(expected, participants):
        return {
            normalized_text(
                template.format(
                    participant=participant, object=object_name, action=action
                )
            )
            for participant in participants
            for object_name in expected.object.names
            for action in expected.actions
            for template in expected.templates
        }

    def event_match(expected, actual):
        if actual is None:
            return False
        # The object reference may anchor the physical target of an event (for
        # example a container's return). It must be the declared object, rather
        # than a different person/item or an invented event identity.
        if actual.document_id is not None and (
            expected.object.document_id is None
            or resolve_expected_id(expected.object.document_id)
            != resolve_actual_id(actual.document_id)
        ):
            return False
        surface = normalized_text(actual.surface)
        names = {normalized_text(name) for name in expected.participant.names}
        if surface in event_surfaces(expected, names):
            return True
        if surface in event_surfaces(expected, known_names - names):
            return False
        unqualified = {
            normalized_text(f"{object_name}{particle} {action}")
            for object_name in expected.object.names
            for action in expected.actions
            for particle in ("", "의")
        }
        if expected.allow_unqualified and surface in unqualified:
            return True
        return None

    def entity_match(expected, actual):
        if expected is None:
            return True
        if isinstance(expected, ExpectedEvent):
            return event_match(expected, actual)
        if actual is None:
            return False
        if expected.document_id:
            if actual.document_id is None or resolve_expected_id(
                expected.document_id
            ) != resolve_actual_id(actual.document_id):
                return False
        elif actual.document_id is not None:
            return False
        surface = normalized_text(actual.surface)
        if surface in {normalized_text(name) for name in expected.names}:
            return True
        # A known different entity is a mismatch. An unfamiliar paraphrase or
        # pronoun remains unassessed even if its document reference is correct.
        return False if surface in known_names else None

    def compare(criterion, fact):
        variants = []
        for frame in frames(criterion):
            dimensions = {
                role: (
                    getattr(fact, role) is None
                    if role in frame.absent_roles
                    else entity_match(getattr(frame, role), getattr(fact, role))
                )
                for role in ("actor", "recipient", "object")
            }
            dimensions.update(
                {
                    field: expected is None or getattr(fact, field) == expected
                    for field in ("certainty", "extent", "time")
                    for expected in (getattr(criterion, field),)
                }
            )
            variants.append(dimensions)
        action_known = recognized_action(fact.action, criterion.actions)
        # Prefer an explicitly matching frame, then an unassessed frame. A wrong
        # actor/qualifier common to every frame remains a definite mismatch.
        dimensions = min(
            variants,
            key=lambda variant: (
                any(value is False for value in variant.values()),
                sum(value is None for value in variant.values()),
                sum(value is False for value in variant.values()),
            ),
        )
        return action_known, dimensions

    mismatches, pending, matched_facts = [], [], set()
    if criteria and len(facts) != len(result.facts):
        mismatches.append("구조화 사실 ID 중복")
    if criteria and set().union(*scopes.values()) - facts.keys():
        mismatches.append("제안이 존재하지 않는 구조화 사실을 참조함")
    for criterion in criteria:
        scope = criterion.scope
        key = scope_key(
            scope.kind,
            scope.document_id,
            scope.target_document_id,
            resolve_expected_id,
        )
        comparisons = [
            (fact_id, *compare(criterion, facts[fact_id]))
            for fact_id in scopes.get(key, ())
            if fact_id in facts
        ]
        matches = {
            fact_id
            for fact_id, action_known, dimensions in comparisons
            if action_known and all(value is True for value in dimensions.values())
        }
        if matches:
            matched_facts.update(matches)
            if criterion.forbidden:
                mismatches.append(f"{criterion.name}: 금지 사실")
            continue
        if criterion.forbidden:
            continue
        known = [
            dimensions for _, action_known, dimensions in comparisons if action_known
        ]
        # A different action with matching roles must not hide the wrong roles
        # or qualifiers of the action this criterion actually assesses.
        differences = {
            dimension
            for dimensions in known
            for dimension, value in dimensions.items()
            if value is False
        }
        if known and all(
            any(value is False for value in dimensions.values()) for dimensions in known
        ):
            mismatches.append(f"{criterion.name}: {', '.join(sorted(differences))}")
        else:
            pending.append(f"{criterion.name}: 사실 또는 표현 미확인")
    if criteria:
        pending.extend(
            f"추가 구조화 사실 {index}: 기준 미등록"
            for index, _ in enumerate(sorted(facts.keys() - matched_facts), 1)
        )

    # These statuses describe the limits of the check. They deliberately do not
    # say that a source-backed free-text body has passed semantic verification.
    return {
        **(
            {
                "semantic_claim_mismatches": sorted(set(mismatches)),
                "unreviewed_semantic_claims": sorted(set(pending)),
            }
            if criteria
            else {}
        ),
        "semantic_assessment": {
            "structured_criteria": (
                "not_configured"
                if not criteria
                else "mismatch"
                if mismatches
                else "review_required"
                if pending
                else "matched"
            ),
            "criteria_count": len(criteria),
            "source_entailment": "human_review_pending",
            "free_text_bodies": (
                "human_review_pending"
                if result.document_proposals or result.new_document_proposals
                else "not_applicable"
            ),
        },
    }
