"""원문 복사 차이와 의미 변경·잘못된 마커·모호한 위치를 구분한다."""

import json
from dataclasses import replace
from difflib import SequenceMatcher

import pytest

from app.refresh.models import FactEntity, Outcome
from app.refresh.rules import RefreshRules
from app.refresh.serialization import result_from_payload, result_to_payload
from app.refresh.source_alignment import align_candidate, find_alignment
from tests.refresh.test_validation_repair import (
    candidate_for,
    repair_scenario,
    run_saved,
)

RAW = "임시 거주지 표시와 우물을 돌아가는 길"
ORIGINAL = "임시 거주지라는 표시와 우물을 돌아가는 길"


def test_recent_novel_particle_omission_has_unique_original_span():
    excerpt = "서하는 지도에 " + ORIGINAL + "을 더했다."
    restored, score, start, end = find_alignment(RAW, excerpt)
    assert restored == ORIGINAL
    assert score >= 0.9
    assert excerpt[start:end] == ORIGINAL


@pytest.mark.parametrize(
    ("value", "source"),
    [
        ("백도윤", "백도현"),
        ("파견 기한은 한 달이다", "파견 기한은 두 달이다"),
        ("서명에 동의했다", "서명에 동의하지 않았다"),
        ("되찾았다", "되찾을 것이다"),
        ("임시 거주지 표시는 없다", "임시 거주지 표시는 있다"),
        ("강연오의 신청서", "강연우의 신청서"),
    ],
)
def test_similar_meaning_changes_are_not_restored(value, source):
    assert find_alignment(value, source) is None


def test_repeated_candidates_and_other_marker_are_not_chosen():
    assert find_alignment(RAW, ORIGINAL + ". " + ORIGINAL) is None
    assert find_alignment(RAW, "강연오는 문을 닫고 나왔다.") is None


def test_high_similarity_does_not_require_a_whitelisted_edit():
    assert find_alignment(RAW, ORIGINAL)[0] == ORIGINAL
    restored, score, start, end = find_alignment("문을닫고 나왔다", "문을 닫고 나왔다.")
    assert restored == "문을 닫고 나왔다"
    assert score == 1
    assert (start, end) == (0, len(restored))


@pytest.mark.parametrize(
    "raw",
    [
        "먼서 우물까지 내려가고, 물 긷는 사람들이 밟아 만든 길을 따라 올라가야 했다",
        "먼저 우물까지 내려가고, 물 긷는 사람들이 발아 만든 길을 따라 올라가야 했다",
        "먼저 우물까지 내려가고, 물 긷는 사람들이 밟아 만든 길을 따라 올라가야 한다",
    ],
)
def test_typo_and_inflection_are_restored_including_first_and_last_word(raw):
    original = (
        "먼저 우물까지 내려가고, 물 긷는 사람들이 밟아 만든 길을 따라 올라가야 했다"
    )
    source = "그녀는 " + original + ". 그 말을 기록했다."
    restored, score, start, end = find_alignment(raw, source)
    assert restored == original
    assert score >= 0.9
    assert source[start:end] == original


@pytest.mark.parametrize(
    "raw,original,names",
    [
        (
            "서류의 내용과 봉인을 확인하고 100명의 신청서를 접수했다",
            "서류의 내용과 봉인을 확인하고 101명의 신청서를 접수했다",
            (),
        ),
        (
            "신청서에는 검수관을 만난 두 달 뒤 반환 신청을 했다고 적혀 있었다",
            "신청서에는 검수관을 만난 한 달 뒤 반환 신청을 했다고 적혀 있었다",
            (),
        ),
        (
            "서류의 내용과 봉인을 확인하고 신청서가 없다고 기록했다",
            "서류의 내용과 봉인을 확인하고 신청서가 있다고 기록했다",
            (),
        ),
        (
            "검수관은 서류의 내용과 봉인을 확인하고 반환을 승인했다",
            "검수관은 서류의 내용과 봉인을 확인하고 반환을 승인하지 않았다",
            (),
        ),
        (
            "민해주는 서류의 내용과 봉인을 확인하고 반환 신청을 접수했다",
            "민해준은 서류의 내용과 봉인을 확인하고 반환 신청을 접수했다",
            ("민해주", "민해준"),
        ),
    ],
)
def test_high_similarity_still_checks_numbers_negation_and_known_names(
    raw, original, names
):
    assert (
        SequenceMatcher(
            None, raw.replace(" ", ""), original.replace(" ", ""), autojunk=False
        ).ratio()
        >= 0.9
    )
    assert find_alignment(raw, original, protected_names=names) is None


def test_exact_match_and_idempotency_keep_previous_candidate():
    scenario = repair_scenario()
    candidate = candidate_for(scenario)
    assert (
        align_candidate(scenario.snapshot, candidate, scenario.model_input) == candidate
    )


def whitespace_candidate():
    scenario = repair_scenario()
    valid = candidate_for(scenario)
    fact = valid.facts[0]
    # 공백을 포함한 기존 사실의 object를 골라 실제 서버 마커와 서비스로 검증한다.
    original = " ".join(fact.evidence[0].quote.split()[:3])
    assert original in fact.evidence[0].quote
    raw = original.replace(" ", "")
    assert raw not in fact.evidence[0].quote
    changed = replace(
        fact, object=FactEntity(raw, None), statement=fact.statement + " " + raw
    )
    return scenario, replace(valid, facts=(changed,)), original, raw


def test_service_accepts_copy_correction_without_second_model_call_and_stores_audit():
    scenario, candidate, original, raw = whitespace_candidate()
    scenario.model.responses.append(candidate)
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert result.usage.calls == 1
    fact = result.facts[0]
    assert fact.object.surface == original
    assert raw not in fact.statement
    assert fact.source_alignments[0].original == raw
    audit = fact.source_alignments[0]
    document = next(
        d
        for d in scenario.snapshot.documents
        if d.document_id == fact.evidence[0].document_id
    )
    assert document.body_text[audit.start : audit.end] == original
    payload = result_to_payload(scenario.request, result, scenario.snapshot)
    stored = result_from_payload(json.loads(json.dumps(payload)))
    assert stored.facts[0].source_alignments == fact.source_alignments
    aligned = align_candidate(scenario.snapshot, candidate, scenario.model_input)
    again = align_candidate(scenario.snapshot, aligned, scenario.model_input)
    assert again.facts[0].source_alignments == aligned.facts[0].source_alignments


def test_false_evidence_cannot_be_fixed_by_similarity():
    scenario, candidate, _, _ = whitespace_candidate()
    fact = candidate.facts[0]
    broken = replace(fact.evidence[0], quote="가짜 원문")
    candidate = replace(candidate, facts=(replace(fact, evidence=(broken,)),))
    assert (
        not align_candidate(scenario.snapshot, candidate, scenario.model_input)
        .facts[0]
        .source_alignments
    )
    result = RefreshRules().validate_candidate(
        scenario.snapshot, candidate, scenario.model_input
    )
    assert result.outcome == Outcome.FAILED


def test_service_accepts_general_typo_without_a_repair_and_keeps_source_audit():
    scenario = repair_scenario()
    valid = candidate_for(scenario)
    fact = valid.facts[0]
    original = "민해주는 백도윤에게 방문"
    raw = original.replace("방문", "뱅문")
    changed = replace(
        fact, object=FactEntity(raw, None), statement=fact.statement + " " + raw
    )
    scenario.model.responses.append(replace(valid, facts=(changed,)))
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert result.usage.calls == 1
    audit = result.facts[0].source_alignments[0]
    assert audit.original == raw
    assert audit.restored == original
    assert audit.similarity >= 0.9


def test_summary_wording_does_not_block_source_correction_or_trigger_repair():
    scenario, candidate, _, raw = whitespace_candidate()
    fact = replace(candidate.facts[0], statement="원래 요약에 표현이 없다.")
    candidate = replace(candidate, facts=(fact,))
    aligned = align_candidate(scenario.snapshot, candidate, scenario.model_input)
    assert aligned.facts[0].object.surface != raw
    assert aligned.facts[0].statement == fact.statement
    scenario.model.responses.append(candidate)
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert result.usage.calls == 1


def test_related_description_tracks_corrected_fact_without_masking_bad_description():
    from app.refresh.facts import fact_description
    from app.refresh.models import RelationProposal

    scenario, candidate, _, _ = whitespace_candidate()
    fact = candidate.facts[0]
    relation = RelationProposal(
        "haeju",
        "doyun",
        3,
        4,
        "related_character",
        "related_character",
        fact.statement,
        fact.evidence,
        fact_ids=(fact.fact_id,),
    )
    candidate = replace(candidate, relation_proposals=(relation,))
    aligned = align_candidate(scenario.snapshot, candidate, scenario.model_input)
    assert aligned.relation_proposals[0].description == fact_description(aligned.facts)
    assert aligned.relation_proposals[0].description != relation.description
    invalid = replace(relation, description="원래부터 사실과 다른 설명")
    aligned = align_candidate(
        scenario.snapshot,
        replace(candidate, relation_proposals=(invalid,)),
        scenario.model_input,
    )
    assert aligned.relation_proposals[0] == invalid
    result = RefreshRules().validate_candidate(
        scenario.snapshot,
        aligned,
        replace(
            scenario.model_input,
            target_ids=scenario.snapshot.target_ids,
            relation_scope=None,
        ),
    )
    assert result.failure.code == "INVALID_RELATION_FACT"


def test_loaded_document_reference_does_not_require_a_surface_name_match():
    scenario = repair_scenario()
    candidate = candidate_for(scenario)
    fact = candidate.facts[0]
    invalid = replace(fact, actor=replace(fact.actor, document_id="yunjun"))
    result = RefreshRules().validate_candidate(
        scenario.snapshot,
        replace(candidate, facts=(invalid,)),
        replace(
            scenario.model_input,
            target_ids=scenario.snapshot.target_ids,
            relation_scope=None,
        ),
    )
    assert result.outcome == Outcome.PROPOSED
    assert result.diagnostics == ()
    assert result.facts[0].actor.document_id == "yunjun"


def test_repeated_noise_does_not_block_a_unique_match_but_duplicate_spans_do():
    assert find_alignment("가나 다라", "가" * 1000 + "가나 다라") is None
    assert find_alignment("가나다라", "가 " * 1000 + "가나 다라")[0] == "가나 다라"
    assert find_alignment("가나다라", "가나 다라. 가나 다라.") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"start": -1},
        {"end": 999999},
        {"similarity": 0.2},
        {"restored": "없는 표현"},
        {"field": "unknown"},
    ],
)
def test_stored_alignment_audit_rejects_tampered_metadata(changes):
    scenario, candidate, _, _ = whitespace_candidate()
    scenario.model.responses.append(candidate)
    _, result = run_saved(scenario)
    payload = json.loads(
        json.dumps(result_to_payload(scenario.request, result, scenario.snapshot))
    )
    payload["facts"][0]["source_alignments"][0].update(changes)
    with pytest.raises(ValueError, match="Invalid stored source alignment"):
        result_from_payload(payload)
