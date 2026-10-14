"""부분 보완에서 무관한 본문·관계·근거의 손실과 검증 우회를 막는다."""

from dataclasses import replace

import pytest

from app.refresh.models import (
    ModelCandidate,
    NewDocumentProposal,
    Outcome,
    RelationProposal,
    ValidationDiagnostic,
)
from app.refresh.repair import preserve_unaffected_proposals
from app.refresh.rules import RefreshRules
from tests.refresh.test_validation_repair import (
    candidate_for,
    invalid_identity,
    repair_scenario,
    run_saved,
)


class CombinedRules(RefreshRules):
    def model_inputs(self, snapshot, chunks):
        return (self.build_prompt(self.build_context(snapshot, chunks)),)


def candidates():
    scenario = repair_scenario(target_ids=("haeju", "doyun"), rules=CombinedRules())
    valid = candidate_for(scenario)
    good = candidate_for(scenario, "doyun", value="백도윤의 파견 기한은 한 달이다.")
    broken = invalid_identity(valid)
    broken_fact = replace(broken.facts[0], fact_id="broken-fact")
    broken_proposal = replace(broken.document_proposals[0], fact_ids=("broken-fact",))
    previous = replace(
        broken,
        document_proposals=(broken_proposal,) + good.document_proposals,
        facts=(broken_fact,) + good.facts,
    )
    diagnostics = (
        ValidationDiagnostic(
            "facts[0].actor.document_id",
            "fact.entity_reference_known",
            "broken-fact",
        ),
    )
    return scenario, previous, valid, good, diagnostics


def test_service_restores_unrelated_period_and_evidence_after_repair_drops_document():
    scenario, previous, repaired, good, _ = candidates()
    scenario.model.responses.extend((previous, repaired))
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert set(result.document_proposals) == set(
        repaired.document_proposals + good.document_proposals
    )
    assert result.usage.calls == 2
    assert result.facts == good.facts
    assert result.attempts[0].outcome == Outcome.FAILED


def test_preservation_rejects_unrelated_rewrite_but_keeps_repaired_usage():
    _, previous, repaired, good, diagnostics = candidates()
    unrelated = replace(good.document_proposals[0], value="백도윤은 배달부다.")
    repaired = replace(
        repaired, document_proposals=repaired.document_proposals + (unrelated,)
    )
    merged = preserve_unaffected_proposals(previous, repaired, diagnostics)
    assert merged.document_proposals[-1] == good.document_proposals[0]
    assert merged.usage == repaired.usage


@pytest.mark.parametrize(
    "rule", ["schema.field_valid", "fact.identifier_unique", "source.original_matches"]
)
def test_unscoped_failures_keep_whole_candidate_repair(rule):
    _, previous, repaired, _, diagnostics = candidates()
    diagnostics = (replace(diagnostics[0], rule=rule),)
    assert preserve_unaffected_proposals(previous, repaired, diagnostics) is repaired


def test_restored_proposal_is_revalidated_instead_of_assumed_valid():
    scenario, previous, repaired, _, diagnostics = candidates()
    invalid = replace(previous.document_proposals[1], base_revision_no=999)
    previous = replace(
        previous, document_proposals=(previous.document_proposals[0], invalid)
    )
    merged = preserve_unaffected_proposals(previous, repaired, diagnostics)
    result = RefreshRules().validate_candidate(
        scenario.snapshot,
        merged,
        replace(scenario.model_input, target_ids=scenario.snapshot.target_ids),
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "REVISION_MISMATCH"


def test_duplicate_previous_fact_ids_disable_partial_preservation():
    _, previous, repaired, _, diagnostics = candidates()
    previous = replace(previous, facts=previous.facts + previous.facts[:1])
    assert preserve_unaffected_proposals(previous, repaired, diagnostics) is repaired


def test_non_candidate_parser_failure_can_be_fully_repaired():
    assert preserve_unaffected_proposals(
        '{"facts":', ModelCandidate(()), ()
    ) == ModelCandidate(())


@pytest.mark.parametrize("kind", ["new_document_proposals", "relation_proposals"])
def test_unrelated_new_setting_and_relationship_restore_their_fact_dependencies(kind):
    _, previous, repaired, good, diagnostics = candidates()
    fact = good.facts[0]
    proposal = (
        NewDocumentProposal(
            "new:EVENT:방문 전달 업무 분담",
            "EVENT",
            "방문 전달 업무 분담",
            fact.statement,
            fact.evidence,
            (fact.fact_id,),
        )
        if kind == "new_document_proposals"
        else RelationProposal(
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
    )
    previous = replace(previous, **{kind: (proposal,)})
    merged = preserve_unaffected_proposals(previous, repaired, diagnostics)
    assert getattr(merged, kind) == (proposal,)
    assert fact in merged.facts


def test_proposals_sharing_an_error_fact_remain_editable():
    _, previous, repaired, _, diagnostics = candidates()
    dependent = replace(previous.document_proposals[1], fact_ids=("broken-fact",))
    previous = replace(
        previous, document_proposals=(previous.document_proposals[0], dependent)
    )
    merged = preserve_unaffected_proposals(previous, repaired, diagnostics)
    assert merged.document_proposals == repaired.document_proposals
