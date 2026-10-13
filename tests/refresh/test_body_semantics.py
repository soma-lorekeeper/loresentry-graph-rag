from dataclasses import replace

import pytest

from app.refresh.facts import fact_description
from app.refresh.models import FactEntity, Failure, Outcome, RefreshResult
from app.refresh.rules import RefreshRules
from evaluation.cases import assess, evaluation_outcome, new_setting_cases
from evaluation.novel import novel_settings_case
from evaluation.runner import (
    CallBudget,
    assemble,
    evaluation_snapshot,
    planned_calls,
    run_case,
)
from evaluation.semantic_cases import (
    boundary_cases,
    rebase_expected_sources,
    semantic_cases,
)
from evaluation.semantics import BodyCriterion, ProposalScope


def story(name):
    return next(
        case
        for case in semantic_cases("offline", "body-test")
        + boundary_cases("offline", "body-test")
        if case.name == name
    )


def accepted(case, candidate=None):
    """본문 오류 테스트도 source/참조 검증은 현재 v5 그대로 적용한다."""
    rules = RefreshRules()
    snapshot = evaluation_snapshot(case)
    model_input = rules.model_inputs(snapshot, rules.chunk(snapshot))[0]
    result = rules.validate_candidate(
        snapshot,
        candidate or case.expected,
        replace(model_input, target_ids=snapshot.target_ids, relation_scope=None),
    )
    assert result.failure is None
    return result


def body_candidate(case, target, body):
    return replace(
        case.expected,
        document_proposals=tuple(
            replace(proposal, value=body)
            if proposal.target_document_id == target
            else proposal
            for proposal in case.expected.document_proposals
        ),
    )


@pytest.mark.parametrize(
    ("name", "target", "body"),
    [
        ("semantic-roles", "carrier", "라민이 태오에게 기억 물통을 운반했다."),
        ("semantic-roles-reordered", "carrier", "시우가 기억 물통을 운반했다."),
        ("semantic-partial-recipient", "mother", "은서는 모든 기억을 되찾았다."),
        ("semantic-partial-recipient", "son", "건우는 가족 기억을 돌려받았다."),
        (
            "semantic-record-and-consent",
            "approver",
            "상우는 기억 물통 반환을 승인했다.",
        ),
        (
            "semantic-object-properties",
            "box",
            "나무 상자에는 이름과 반환 번호 라벨이 붙어 있다.",
        ),
        (
            "semantic-object-properties",
            "glass",
            "유리통은 구리로 봉인되어 있다.",
        ),
        (
            "semantic-anonymous-attribution",
            "item-envelope",
            "연서가 방문객에게 녹색 봉투를 건넸다.",
        ),
    ],
)
def test_source_checked_facts_cannot_hide_registered_free_body_errors(
    name, target, body
):
    case = story(name)
    assessment = assess(case, accepted(case, body_candidate(case, target, body)))
    assert not assessment["semantic_claim_mismatches"]
    assert assessment["body_claim_mismatches"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"
    assert (
        assessment["body_assessment"]["semantic_entailment"] == "human_review_pending"
    )


def test_correct_and_wrong_clauses_do_not_hide_each_other():
    case = story("semantic-partial-recipient")
    body = "은서는 가족 기억 일부를 되찾았다. 은서는 모든 기억을 되찾았다."
    assessment = assess(case, accepted(case, body_candidate(case, "mother", body)))
    assert assessment["body_claim_mismatches"]


@pytest.mark.parametrize(
    ("name", "target", "body"),
    [
        ("semantic-partial-recipient", "mother", "은서는 가족 기억을 되찾았다."),
        (
            "semantic-record-and-consent",
            "approver",
            "상우는 내일 기억 물통 반환을 승인할 예정이다.",
        ),
        (
            "semantic-anonymous-attribution",
            "character-yeonseo",
            "연서는 전시 안내소에서 보관함을 정리했다.",
        ),
    ],
)
def test_missing_partial_condition_or_negation_expression_is_review_not_semantic_pass(
    name, target, body
):
    case = story(name)
    assessment = assess(case, accepted(case, body_candidate(case, target, body)))
    assert not assessment["body_claim_mismatches"]
    assert assessment["unreviewed_body_claims"]
    assert evaluation_outcome({"assessment": assessment}) == "review_required"


def test_unregistered_correct_paraphrase_is_reviewed_without_false_failure():
    case = story("semantic-partial-recipient")
    body = "은서의 가족 기억 중 작은 몫만 되돌아왔고 나머지는 돌아오지 않았다."
    assessment = assess(case, accepted(case, body_candidate(case, "mother", body)))
    assert not assessment["body_claim_mismatches"]
    assert assessment["body_assessment"]["registered_patterns"] == "review_required"
    assert assessment["body_assessment"]["fact_alignment"] == "human_review_pending"


def test_registered_pattern_matches_never_prove_whole_body_or_fact_alignment():
    case = story("semantic-roles")
    assessment = assess(case, accepted(case))
    assert not assessment["body_claim_mismatches"]
    assert not assessment["unreviewed_body_claims"]
    assert assessment["body_assessment"]["registered_patterns"] == "matched_patterns"
    assert (
        assessment["body_assessment"]["semantic_entailment"] == "human_review_pending"
    )
    assert assessment["body_assessment"]["fact_alignment"] == "human_review_pending"
    assert (
        assessment["semantic_assessment"]["free_text_bodies"] == "human_review_pending"
    )


def test_failed_atomic_discard_is_unassessed_rather_than_extraction_omission():
    case = story("semantic-roles")
    result = RefreshResult(
        case.request.job, Outcome.FAILED, failure=Failure("INVALID_FACT", "Rejected")
    )
    assessment = assess(case, result)
    assert assessment["assessment_availability"] == "unavailable_final_failure"
    assert assessment["omission_metrics"] == "not_evaluated"
    for key in ("missing_targets", "missing_new_settings", "missing_relations"):
        assert key not in assessment
    assert assessment["body_assessment"]["registered_patterns"] == "not_evaluated"
    assert assessment["semantic_assessment"]["structured_criteria"] == "not_evaluated"
    assert (
        evaluation_outcome(
            {"result": {"error": {"code": "INVALID_FACT"}}, "assessment": assessment}
        )
        == "execution_failed"
    )


def test_unexpected_empty_success_still_detects_required_omissions():
    case = story("semantic-roles")
    assessment = assess(case, RefreshResult(case.request.job, Outcome.NO_CHANGE))
    assert assessment["assessment_availability"] == "available"
    assert assessment["omission_metrics"] == "evaluated"
    assert assessment["missing_targets"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize("index", range(3))
def test_boundary_offline_fixtures_exercise_current_rules_without_claiming_live_quality(
    index,
):
    case = boundary_cases("offline", "boundary-test")[index]
    result, assessment = run_case(
        case, assemble(case, None, CallBudget(planned_calls(case)))
    )
    assert result.failure is None
    assert result.outcome == (Outcome.NO_CHANGE if index == 2 else Outcome.PROPOSED)
    assert not assessment.get("body_claim_mismatches")
    assert not assessment.get("semantic_claim_mismatches")
    assert not assessment.get("missing_targets")
    if index == 2:
        assert assessment["body_assessment"]["semantic_entailment"] == "not_applicable"
        assert evaluation_outcome({"assessment": assessment}) == "passed"


def test_boundary_pronoun_and_antecedent_are_split_without_fabricated_role_binding():
    case = story("boundary-pronoun-owner")
    chunks = RefreshRules().chunk(evaluation_snapshot(case))
    fact = case.expected.facts[0]
    assert fact.actor == FactEntity("그녀", None)
    assert any("미라" in c.evidence.quote for c in chunks)
    assert "미라" not in fact.evidence[0].quote
    assert "그녀" in fact.evidence[0].quote
    assert not any(
        "미라" in c.evidence.quote and "보관했다" in c.evidence.quote for c in chunks
    )


@pytest.mark.parametrize("chunk_chars", [160, 200, 1000])
def test_fixed_evidence_rebase_matches_current_chunking_and_preserves_golden_meaning(
    chunk_chars,
):
    original = story("boundary-pronoun-owner")
    adjusted = replace(
        original,
        request=replace(
            original.request,
            settings=replace(original.request.settings, chunk_chars=chunk_chars),
        ),
    )
    rebased = rebase_expected_sources(adjusted)
    assert rebased.source == original.source
    assert rebased.request.job == original.request.job
    assert rebased.body_criteria == original.body_criteria
    assert rebased.expected.facts[0].actor == original.expected.facts[0].actor
    assert rebased.expected.facts[0].statement == original.expected.facts[0].statement
    chunks = RefreshRules().chunk(evaluation_snapshot(rebased))
    assert rebased.expected.facts[0].evidence[0] in {c.evidence for c in chunks}
    assert accepted(rebased).outcome == Outcome.PROPOSED


def test_boundary_named_frame_is_accepted_only_with_runtime_source_and_identity_checks():
    case = story("boundary-pronoun-owner")
    case = rebase_expected_sources(
        replace(
            case,
            request=replace(
                case.request, settings=replace(case.request.settings, chunk_chars=200)
            ),
        )
    )
    fact = replace(
        case.expected.facts[0],
        actor=FactEntity("미라", "owner"),
        statement="미라는 파란 열쇠를 보관했다.",
    )
    candidate = replace(
        case.expected,
        facts=(fact,),
        relation_proposals=tuple(
            replace(proposal, description=fact_description((fact,)))
            for proposal in case.expected.relation_proposals
        ),
    )
    assessment = assess(case, accepted(case, candidate))
    assert not assessment["semantic_claim_mismatches"]
    assert not assessment["unreviewed_semantic_claims"]


def test_wrong_source_chunk_does_not_become_a_semantic_omission_or_pass():
    case = story("boundary-pronoun-owner")
    rules = RefreshRules()
    snapshot = evaluation_snapshot(case)
    chunks = rules.chunk(snapshot)
    wrong = next(
        c.evidence
        for c in chunks
        if "보관했다" not in c.evidence.quote and c.evidence.document_id == "m1"
    )
    candidate = replace(
        case.expected, facts=(replace(case.expected.facts[0], evidence=(wrong,)),)
    )
    first = rules.model_inputs(snapshot, chunks)[0]
    result = rules.validate_candidate(
        snapshot,
        candidate,
        replace(first, target_ids=snapshot.target_ids, relation_scope=None),
    )
    assert result.failure.code == "INVALID_FACT"
    assessment = assess(case, result)
    assert assessment["omission_metrics"] == "not_evaluated"


def test_unresolvable_source_actor_remains_unresolved_and_named_guess_fails():
    case = story("boundary-ambiguous-actor")
    assert accepted(case).failure is None
    fact = replace(
        case.expected.facts[0],
        actor=FactEntity("나린", "narin"),
        statement="나린이 기록 봉투를 건넸다.",
    )
    assessment = assess(case, accepted(case, replace(case.expected, facts=(fact,))))
    assert assessment["semantic_claim_mismatches"]


@pytest.mark.parametrize(
    ("target", "body"),
    [
        ("character-doyun", "반장의 명찰 뒤에는 임시 파견 종이가 붙어 있었다."),
        ("character-doyun", "백도윤은 두 달 동안 임시 파견되어 방문 전달을 맡는다."),
        ("character-doyun", "백도윤은 반환 승인과 봉인 해제를 맡는다."),
        ("worldview-memory-tax", "기억세는 완전히 폐지되었다."),
    ],
)
def test_novel_registered_body_errors_are_separate_from_legacy_fixed_word_baseline(
    target, body
):
    case = novel_settings_case("offline", "novel-body")
    candidate = body_candidate(case, target, body)
    if not any(p.target_document_id == target for p in candidate.document_proposals):
        from app.refresh.models import DocumentProposal

        candidate = replace(
            candidate,
            document_proposals=candidate.document_proposals
            + (
                DocumentProposal(
                    target,
                    next(
                        d.revision_no for d in case.targets if d.document_id == target
                    ),
                    "body_text",
                    body,
                    (),
                ),
            ),
        )
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        document_proposals=candidate.document_proposals,
        new_document_proposals=candidate.new_document_proposals,
        relation_proposals=candidate.relation_proposals,
    )
    # Historical fixed proposals have no facts and are not current live evidence.
    assessment = assess(case, result)
    assert assessment["body_claim_mismatches"]


def test_body_criteria_validate_scope_patterns_and_unique_names():
    with pytest.raises(ValueError):
        BodyCriterion(
            "bad", ProposalScope("relation", "a", "b"), required_patterns=("x",)
        )
    with pytest.raises(ValueError):
        BodyCriterion("empty", ProposalScope("document", "a"))
    with pytest.raises(ValueError):
        BodyCriterion(
            "empty pattern", ProposalScope("document", "a"), required_patterns=("",)
        )
    case = story("semantic-roles")
    with pytest.raises(ValueError):
        replace(case, body_criteria=case.body_criteria + case.body_criteria)


def test_bad_alias_body_cannot_be_overwritten_by_clean_canonical_body():
    case = new_setting_cases("offline", "body-alias")[2]
    proposal = case.expected.new_document_proposals[0]
    case = replace(
        case,
        body_criteria=(
            BodyCriterion(
                "동의 없는 납부 단정",
                ProposalScope("new", proposal.candidate_id),
                forbidden_patterns=(r"동의는 필요 없다(?=[.!?。]|$)",),
            ),
        ),
    )
    wrong = replace(
        proposal,
        candidate_id="arbitrary-alias-id",
        name="해무시의 기억세",
        body_text="동의는 필요 없다.",
    )
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        new_document_proposals=(wrong, proposal),
    )
    assessment = assess(case, result)
    assert assessment["body_claim_mismatches"]
    detail = assessment["body_assessment"]["details"][0]
    assert detail["bodies_evaluated"] == 2
    assert detail["statuses"] == ["mismatch", "matched_patterns"]


@pytest.mark.parametrize(
    "body",
    [
        "강연오는 등대에서 신청했던 사실도 잊었다. 노래 한 소절을 되찾았지만 모든 기억을 되찾지는 못했다. 정확한 가사는 여전히 기억하지 못한다.",
        "강연오는 등대에서 신청했던 사실도 잊었다. 딸이 살아 있던 때의 노래 한 소절을 되찾았다. 정확한 가사는 여전히 기억하지 못한다.",
        "강연오는 등대에서 신청했던 사실도 잊었다. 노래 한 소절이 돌아왔지만 기억을 완전히 회복하지는 못했다. 정확한 가사는 기억하지 못한다.",
    ],
)
def test_novel_negative_and_historical_mentions_are_not_completion_or_resurrection(
    body,
):
    case = novel_settings_case("offline", "body-negative")
    original = case.expected.new_document_proposals[0]
    assert original.name == "강연오"
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        new_document_proposals=(replace(original, body_text=body),),
    )
    assessment = assess(case, result)
    assert assessment["new_setting_content_valid"]
    assert not assessment["body_claim_mismatches"]


@pytest.mark.parametrize(
    "claim",
    ["모든 기억을 되찾았다.", "딸이 살아 돌아왔다.", "정확한 가사를 되찾았다."],
)
def test_novel_affirmative_complete_recovery_resurrection_or_lyrics_claim_still_fails(
    claim,
):
    case = novel_settings_case("offline", "body-positive-wrong")
    original = case.expected.new_document_proposals[0]
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        new_document_proposals=(
            replace(
                original,
                body_text=f"강연오는 등대에서 신청했던 일을 잊었다. 노래 한 소절을 되찾았다. {claim}",
            ),
        ),
    )
    assert assess(case, result)["body_claim_mismatches"]


def test_negated_or_quoted_wrong_action_is_not_registered_affirmative_action():
    case = story("semantic-record-and-consent")
    body = "상우는 기억 물통 반환을 승인했다는 거짓 기록을 정정했다. 현재 동의를 확인한 후 내일 승인할 예정이다."
    assessment = assess(case, accepted(case, body_candidate(case, "approver", body)))
    assert not assessment["body_claim_mismatches"]
    assert not assessment["unreviewed_body_claims"]
