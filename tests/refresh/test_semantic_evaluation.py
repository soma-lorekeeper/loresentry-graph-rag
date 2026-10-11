from dataclasses import replace

import pytest

from app.refresh.facts import fact_description
from app.refresh.models import Evidence, FactEntity, Outcome, RefreshResult, SourceFact
from app.refresh.rules import RefreshRules
from evaluation.cases import assess, evaluation_outcome, new_setting_cases
from evaluation.runner import (
    CallBudget,
    assemble,
    evaluation_snapshot,
    planned_calls,
    run_case,
)
from evaluation.semantic_cases import semantic_cases
from evaluation.semantics import (
    ExpectedEntity,
    ExpectedEvent,
    ProposalScope,
    SemanticCriterion,
    SemanticFrame,
)


def story(name):
    return next(
        case
        for case in semantic_cases("gpt-5.6-luna", "semantic-test")
        if case.name == name
    )


def with_facts(case, facts, relation_refs=None):
    fact_map = {fact.fact_id: fact for fact in facts}

    def evidence(refs):
        return tuple(dict.fromkeys(e for key in refs for e in fact_map[key].evidence))

    relations = []
    for proposal in case.expected.relation_proposals:
        pair = frozenset((proposal.document_id, proposal.target_document_id))
        refs = (relation_refs or {}).get(pair, proposal.fact_ids)
        relations.append(
            replace(
                proposal,
                fact_ids=refs,
                description=fact_description(fact_map[key] for key in refs),
                evidence=evidence(refs),
            )
        )
    return replace(
        case.expected,
        facts=facts,
        document_proposals=tuple(
            replace(proposal, evidence=evidence(proposal.fact_ids))
            for proposal in case.expected.document_proposals
        ),
        relation_proposals=tuple(relations),
    )


def validated(case, candidate):
    """현재 v5의 엄격한 규칙을 통과한 후보에서도 의미 기준의 한계를 검사한다."""
    rules = RefreshRules()
    snapshot = evaluation_snapshot(case)
    first = rules.model_inputs(snapshot, rules.chunk(snapshot))[0]
    result = rules.validate_candidate(
        snapshot,
        candidate,
        replace(
            first,
            target_ids=snapshot.target_ids,
            relation_scope=None,
            schema_version="refresh-candidate-v5",
        ),
    )
    assert result.failure is None
    return result


@pytest.mark.parametrize(
    "name", [case.name for case in semantic_cases("gpt-5.6-luna", "names")]
)
def test_offline_semantic_fixtures_check_structured_criteria_and_leave_human_review(
    name,
):
    case = story(name)
    result, assessment = run_case(
        case, assemble(case, None, CallBudget(planned_calls(case)))
    )
    assert result.outcome == Outcome.PROPOSED
    assert assessment["rules_accepted"]
    assert not assessment["semantic_claim_mismatches"]
    assert not assessment["unreviewed_semantic_claims"]
    assert assessment["semantic_assessment"] == {
        "structured_criteria": "matched",
        "criteria_count": len(case.semantic_criteria),
        "source_entailment": "human_review_pending",
        "free_text_bodies": "human_review_pending",
    }
    assert not case.required_words


@pytest.mark.parametrize("name", ["semantic-roles", "semantic-roles-reordered"])
def test_actor_and_recipient_swap_is_detected_across_different_names_and_sentence_order(
    name,
):
    case = story(name)
    transport = case.expected.facts[1]
    wrong = replace(transport, actor=transport.recipient, recipient=transport.actor)
    facts = tuple(
        wrong if fact.fact_id == wrong.fact_id else fact for fact in case.expected.facts
    )
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert any(
        "운반자와 수령인" in item and "actor" in item and "recipient" in item
        for item in assessment["semantic_claim_mismatches"]
    )
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_different_action_with_correct_roles_cannot_mask_known_action_with_wrong_roles():
    case = story("semantic-roles")
    transport = case.expected.facts[1]
    wrong = replace(transport, actor=transport.recipient, recipient=transport.actor)
    other_action = replace(transport, fact_id="other-action", action="운반")
    facts = tuple(
        wrong if fact.fact_id == wrong.fact_id else fact for fact in case.expected.facts
    ) + (other_action,)
    candidate = with_facts(
        case, facts, {frozenset(("carrier", "receiver")): ("transport", "other-action")}
    )
    assessment = assess(case, validated(case, candidate))
    assert any(
        "운반자와 수령인" in item for item in assessment["semantic_claim_mismatches"]
    )
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize("forbidden", [False, True])
def test_wrong_extra_claim_cannot_be_hidden_by_a_correct_claim(forbidden):
    case = story("semantic-roles")
    transport = case.expected.facts[1]
    wrong = replace(
        transport,
        fact_id="wrong-extra",
        actor=transport.recipient,
        recipient=transport.actor,
    )
    if forbidden:
        criterion = replace(
            case.semantic_criteria[1],
            name="역할 뒤집기 금지",
            actor=case.semantic_criteria[1].recipient,
            recipient=case.semantic_criteria[1].actor,
            forbidden=True,
        )
        case = replace(case, semantic_criteria=case.semantic_criteria + (criterion,))
    candidate = with_facts(
        case,
        case.expected.facts + (wrong,),
        {frozenset(("carrier", "receiver")): ("transport", "wrong-extra")},
    )
    assessment = assess(case, validated(case, candidate))
    assert evaluation_outcome({"assessment": assessment}) == (
        "failed" if forbidden else "review_required"
    )


def test_relation_recipient_must_agree_with_the_body_fact_and_source_criterion():
    case = story("semantic-partial-recipient")
    original = case.expected.facts[0]
    source = case.source.documents[0]
    wrong = replace(
        original,
        fact_id="wrong-relation-recipient",
        recipient=FactEntity("건우", "son"),
        statement="건우는 가족 기억 일부를 돌려받았다.",
        evidence=(Evidence("m1", 3, 0, len(source.body_text), source.body_text),),
    )
    candidate = with_facts(
        case,
        case.expected.facts + (wrong,),
        {frozenset(("mother", "memory")): (wrong.fact_id,)},
    )
    assessment = assess(case, validated(case, candidate))
    assert any(
        "실제 반환 수령인" in item and "recipient" in item
        for item in assessment["semantic_claim_mismatches"]
    )
    assert not any(
        "본문의 반환 수령인" in item for item in assessment["semantic_claim_mismatches"]
    )
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize(
    "key,change,dimension",
    [
        ("partial", {"extent": "COMPLETE"}, "extent"),
        ("future-approval", {"certainty": "CONFIRMED"}, "certainty"),
        ("future-approval", {"time": "CURRENT"}, "time"),
        ("past-record", {"time": "CURRENT"}, "time"),
    ],
)
def test_partial_state_and_explicit_time_or_modality_errors_are_detected(
    key, change, dimension
):
    case = story(
        "semantic-partial-recipient"
        if key == "partial"
        else "semantic-record-and-consent"
    )
    facts = tuple(
        replace(fact, **change) if fact.fact_id == key else fact
        for fact in case.expected.facts
    )
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert any(dimension in item for item in assessment["semantic_claim_mismatches"])
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_documented_record_is_distinct_from_actual_return_and_does_not_force_reported_certainty():
    case = story("semantic-record-and-consent")
    for certainty in ("CONFIRMED", "REPORTED"):
        facts = tuple(
            replace(fact, certainty=certainty)
            if fact.fact_id == "past-record"
            else fact
            for fact in case.expected.facts
        )
        assessment = assess(case, validated(case, with_facts(case, facts)))
        assert not assessment["semantic_claim_mismatches"]
        assert not assessment["unreviewed_semantic_claims"]


@pytest.mark.parametrize(
    "key,actor,statement",
    [
        (
            "label",
            FactEntity("나무 상자", "box"),
            "나무 상자에는 이름과 반환 번호 라벨이 붙어 있다.",
        ),
        ("seal", FactEntity("유리통", "glass"), "유리통은 구리 봉인으로 잠겨 있다."),
    ],
)
def test_object_properties_cannot_be_transferred_between_container_and_contents(
    key, actor, statement
):
    case = story("semantic-object-properties")
    source = case.source.documents[0]
    facts = tuple(
        replace(
            fact,
            actor=actor,
            statement=statement,
            evidence=(Evidence("m1", 3, 0, len(source.body_text), source.body_text),),
        )
        if fact.fact_id == key
        else fact
        for fact in case.expected.facts
    )
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert any("actor" in item for item in assessment["semantic_claim_mismatches"])
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize(
    "action,expected",
    [
        ("기억 물통을 운반한다", "matched"),
        ("운반", "review_required"),
        ("안 운반한다", "review_required"),
        ("운반한다는 허위 기록이었다", "review_required"),
    ],
)
def test_long_complete_action_and_unregistered_or_negated_expressions_are_distinguished(
    action, expected
):
    case = story("semantic-roles")
    facts = tuple(
        replace(fact, action=action) if fact.fact_id == "transport" else fact
        for fact in case.expected.facts
    )
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        document_proposals=case.expected.document_proposals,
        relation_proposals=case.expected.relation_proposals,
        facts=facts,
    )
    assessment = assess(case, result)
    assert not assessment["semantic_claim_mismatches"]
    assert assessment["semantic_assessment"]["structured_criteria"] == expected


def test_missing_explicit_document_reference_is_a_structured_role_mismatch():
    case = story("semantic-roles")
    facts = tuple(
        replace(fact, actor=replace(fact.actor, document_id=None))
        if fact.fact_id == "transport"
        else fact
        for fact in case.expected.facts
    )
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert any("actor" in item for item in assessment["semantic_claim_mismatches"])


def test_legacy_free_text_and_unreferenced_facts_are_unassessed_instead_of_semantically_passed():
    case = story("semantic-roles")
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        document_proposals=tuple(
            replace(p, fact_ids=()) for p in case.expected.document_proposals
        ),
        relation_proposals=tuple(
            replace(p, fact_ids=()) for p in case.expected.relation_proposals
        ),
    )
    for facts in ((), case.expected.facts):
        assessment = assess(case, replace(result, facts=facts))
        assert evaluation_outcome({"assessment": assessment}) == "review_required"
        assert (
            assessment["semantic_assessment"]["source_entailment"]
            == "human_review_pending"
        )


def test_free_body_paraphrase_is_not_rejected_or_claimed_semantically_verified():
    case = story("semantic-roles")
    paraphrases = {
        "approver": "라민이 수진의 물통 운반 업무를 허가한다.",
        "carrier": "수진이 태오에게 기억 물통을 가져다준다.",
        "receiver": "태오에게 돌아온 기억은 전부가 아니라 일부다.",
    }
    candidate = replace(
        case.expected,
        document_proposals=tuple(
            replace(
                proposal,
                value=paraphrases[proposal.target_document_id],
            )
            for proposal in case.expected.document_proposals
        ),
    )
    assessment = assess(case, validated(case, candidate))
    assert not assessment["semantic_claim_mismatches"]
    assert assessment["semantic_assessment"]["structured_criteria"] == "matched"
    assert (
        assessment["semantic_assessment"]["free_text_bodies"] == "human_review_pending"
    )


def test_semantic_new_entity_and_scope_use_explicit_name_aliases_and_not_temporary_ids():
    case = new_setting_cases("gpt-5.6-luna", "semantic-alias")[2]
    text = case.source.documents[0].body_text
    fact = SourceFact(
        "memory-tax",
        FactEntity("해무시의 기억세", "new:WORLDVIEW:해무시의 기억세"),
        "납부",
        None,
        None,
        "CONFIRMED",
        "UNSPECIFIED",
        "CURRENT",
        text,
        (Evidence("m1", 3, 0, len(text), text),),
    )
    creation = replace(
        case.expected.new_document_proposals[0],
        candidate_id="new:WORLDVIEW:해무시의 기억세",
        name="해무시의 기억세",
        fact_ids=(fact.fact_id,),
    )
    relation = replace(
        case.expected.relation_proposals[0],
        target_document_id=creation.candidate_id,
        fact_ids=(fact.fact_id,),
        description=fact.statement,
    )
    case = replace(
        case,
        semantic_criteria=(
            SemanticCriterion(
                "기억세 납부",
                ("납부",),
                actor=ExpectedEntity(
                    ("기억세", "해무시의 기억세"), "new:WORLDVIEW:기억세"
                ),
                scope=ProposalScope("new", "new:WORLDVIEW:기억세"),
            ),
        ),
    )
    candidate = replace(
        case.expected,
        new_document_proposals=(creation,),
        relation_proposals=(relation,),
        facts=(fact,),
    )
    assessment = assess(case, validated(case, candidate))
    assert not assessment["semantic_claim_mismatches"]
    assert not assessment["unreviewed_semantic_claims"]


def test_duplicate_fact_ids_cannot_overwrite_a_wrong_claim_with_a_clean_one():
    case = story("semantic-roles")
    original = case.expected.facts[1]
    wrong = replace(original, actor=original.recipient)
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        document_proposals=case.expected.document_proposals,
        relation_proposals=case.expected.relation_proposals,
        facts=(wrong,) + case.expected.facts,
    )
    assessment = assess(case, result)
    assert "구조화 사실 ID 중복" in assessment["semantic_claim_mismatches"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_anonymous_handoff_cannot_be_attributed_to_a_named_bystander():
    case = story("semantic-anonymous-attribution")
    source = case.source.documents[0]
    facts = tuple(
        replace(
            fact,
            actor=FactEntity("연서", "character-yeonseo"),
            statement="연서는 이름표가 없는 방문객에게 녹색 봉투를 건넸다.",
            evidence=(Evidence("m1", 3, 0, len(source.body_text), source.body_text),),
        )
        if fact.fact_id == "anonymous-handoff"
        else fact
        for fact in case.expected.facts
    )
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert any("귀속 금지" in item for item in assessment["semantic_claim_mismatches"])
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize("name", ["semantic-roles", "semantic-roles-reordered"])
@pytest.mark.parametrize("qualified", [False, True])
def test_explicit_approval_event_frames_preserve_the_carrier_identity(name, qualified):
    case = story(name)
    original = case.expected.facts[0]
    carrier = original.recipient
    event = "기억 물통 운반"
    approval = replace(
        original,
        recipient=None if qualified else carrier,
        object=FactEntity(f"{carrier.surface}의 {event}" if qualified else event, None),
    )
    facts = (approval,) + case.expected.facts[1:]
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert not assessment["semantic_claim_mismatches"]
    assert not assessment["unreviewed_semantic_claims"]
    assert assessment["semantic_assessment"]["structured_criteria"] == "matched"
    assert (
        assessment["semantic_assessment"]["source_entailment"] == "human_review_pending"
    )


def test_approval_event_without_a_structured_participant_remains_unassessed():
    case = story("semantic-roles")
    approval = replace(
        case.expected.facts[0],
        recipient=None,
        object=FactEntity("기억 물통 운반", None),
    )
    candidate = with_facts(case, (approval,) + case.expected.facts[1:])
    assessment = assess(case, validated(case, candidate))
    assert not assessment["semantic_claim_mismatches"]
    assert any(
        "운반 승인 담당" in item for item in assessment["unreviewed_semantic_claims"]
    )
    assert evaluation_outcome({"assessment": assessment}) == "review_required"


@pytest.mark.parametrize(
    "change,dimension",
    [
        ("actor", "actor"),
        ("person", "object"),
        ("recipient", "recipient"),
        ("certainty", "certainty"),
    ],
)
def test_approval_event_alternatives_do_not_accept_wrong_people_or_qualifiers(
    change, dimension
):
    case = story("semantic-roles")
    original = case.expected.facts[0]
    approval = replace(
        original,
        recipient=None,
        object=FactEntity(f"{original.recipient.surface}의 기억 물통 운반", None),
    )
    if change == "actor":
        approval = replace(approval, actor=original.recipient)
    elif change == "person":
        other = case.expected.facts[1].recipient
        approval = replace(
            approval, object=FactEntity(f"{other.surface}의 기억 물통 운반", None)
        )
    elif change == "recipient":
        approval = replace(approval, recipient=case.expected.facts[1].recipient)
    else:
        approval = replace(approval, certainty="PLANNED")
    candidate = with_facts(case, (approval,) + case.expected.facts[1:])
    # The wrong event participant is already outside the exact source. Compare
    # the semantic contract directly without loosening the runtime source check.
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        document_proposals=candidate.document_proposals,
        relation_proposals=candidate.relation_proposals,
        facts=candidate.facts,
    )
    assessment = assess(case, result)
    assert any(
        "운반 승인 담당" in item and dimension in item
        for item in assessment["semantic_claim_mismatches"]
    )
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize(
    "dimension,value", [("time", "CURRENT"), ("certainty", "CONFIRMED")]
)
def test_future_approval_event_alternative_still_requires_planned_future_state(
    dimension, value
):
    case = story("semantic-record-and-consent")
    facts = tuple(
        replace(
            fact,
            recipient=None,
            object=FactEntity("예진에게 기억 물통의 반환", None),
            **{dimension: value},
        )
        if fact.fact_id == "future-approval"
        else fact
        for fact in case.expected.facts
    )
    candidate = with_facts(case, facts)
    assessment = assess(
        case,
        RefreshResult(
            case.request.job,
            Outcome.PROPOSED,
            document_proposals=candidate.document_proposals,
            relation_proposals=candidate.relation_proposals,
            facts=candidate.facts,
        ),
    )
    assert any(dimension in item for item in assessment["semantic_claim_mismatches"])


def test_unqualified_return_event_remains_unassessed_and_missing_relationship_still_fails():
    case = story("semantic-record-and-consent")
    facts = tuple(
        replace(fact, recipient=None, object=FactEntity("기억 물통의 반환", None))
        if fact.fact_id == "future-approval"
        else fact
        for fact in case.expected.facts
    )
    candidate = with_facts(case, facts)
    assessment = assess(case, validated(case, candidate))
    assert not assessment["semantic_claim_mismatches"]
    assert any(
        "현재 동의 뒤 승인 예정" in item
        for item in assessment["unreviewed_semantic_claims"]
    )
    candidate = replace(
        candidate,
        relation_proposals=tuple(
            proposal
            for proposal in candidate.relation_proposals
            if frozenset((proposal.document_id, proposal.target_document_id))
            != frozenset(("applicant", "approver"))
        ),
    )
    assessment = assess(case, validated(case, candidate))
    assert assessment["missing_relations"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_semantic_fixture_evidence_is_the_full_source_chunk():
    for case in semantic_cases("gpt-5.6-luna", "full-source"):
        source = case.source.documents[0]
        assert all(
            fact.evidence
            == (Evidence("m1", 3, 0, len(source.body_text), source.body_text),)
            for fact in case.expected.facts
        )


def test_event_frame_configuration_rejects_ambiguous_absence_and_unknown_template_fields():
    with pytest.raises(ValueError, match="Absent roles"):
        SemanticFrame(
            recipient=ExpectedEntity(("참여인",)), absent_roles=("recipient",)
        )
    with pytest.raises(ValueError, match="templates"):
        ExpectedEvent(
            ExpectedEntity(("참여인",)),
            ExpectedEntity(("물품",)),
            ("운반",),
            templates=("{participant}의 {object} {action} {unregistered}",),
        )


@pytest.mark.parametrize(
    "anchor,expected", [("tube", "review_required"), ("applicant", "failed")]
)
def test_return_event_can_anchor_only_the_expected_object_and_keeps_missing_participant_unassessed(
    anchor, expected
):
    case = story("semantic-record-and-consent")
    facts = tuple(
        replace(fact, recipient=None, object=FactEntity("기억 물통의 반환", anchor))
        if fact.fact_id == "future-approval"
        else fact
        for fact in case.expected.facts
    )
    assessment = assess(case, validated(case, with_facts(case, facts)))
    assert evaluation_outcome({"assessment": assessment}) == expected
    if anchor == "tube":
        assert not assessment["semantic_claim_mismatches"]
        assert any(
            "현재 동의 뒤 승인 예정" in item
            for item in assessment["unreviewed_semantic_claims"]
        )
    else:
        assert any(
            "현재 동의 뒤 승인 예정" in item
            for item in assessment["semantic_claim_mismatches"]
        )


@pytest.mark.parametrize(
    "participant,anchor,certainty,time,expected",
    [
        ("예진", "tube", "PLANNED", "FUTURE", "matched"),
        ("상우", "tube", "PLANNED", "FUTURE", "mismatch"),
        ("예진", "approver", "PLANNED", "FUTURE", "mismatch"),
        ("예진", "tube", "CONFIRMED", "FUTURE", "mismatch"),
        ("예진", "tube", "PLANNED", "CURRENT", "mismatch"),
    ],
)
def test_explicit_return_event_anchor_preserves_person_object_and_future_modality(
    participant, anchor, certainty, time, expected
):
    case = story("semantic-record-and-consent")
    facts = tuple(
        replace(
            fact,
            recipient=None,
            object=FactEntity(f"{participant}에게 기억 물통의 반환", anchor),
            certainty=certainty,
            time=time,
        )
        if fact.fact_id == "future-approval"
        else fact
        for fact in case.expected.facts
    )
    candidate = with_facts(case, facts)
    # These event surfaces exercise the evaluator contract independently. They
    # are not added to the fixture source or golden and do not bypass live rules.
    assessment = assess(
        case,
        RefreshResult(
            case.request.job,
            Outcome.PROPOSED,
            document_proposals=candidate.document_proposals,
            relation_proposals=candidate.relation_proposals,
            facts=candidate.facts,
        ),
    )
    assert assessment["semantic_assessment"]["structured_criteria"] == expected
