import json
from dataclasses import replace
from unittest.mock import Mock

from app.adapters.llm import ModelLimits
from app.refresh.models import Outcome
from app.refresh.rules import RefreshRules
from evaluation.artifacts import AttemptRecorder
from evaluation.runner import (
    CallBudget,
    EvaluationModel,
    assemble,
    evaluation_snapshot,
    run_case,
)
from evaluation.semantic_cases import rebase_expected_sources, semantic_cases
from evaluation.source_observation import observe_sources, source_summary
from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_artifacts import setup
from tests.refresh.test_evaluation_runner import arguments


def fixture():
    return rebase_expected_sources(
        semantic_cases("gpt-6-luna", "source-observation")[0]
    )


def test_all_expression_checks_continue_after_multiple_mismatches():
    case = fixture()
    snapshot = evaluation_snapshot(case)
    model_input = RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot))[
        0
    ]
    facts = tuple(
        replace(
            fact,
            action=f"원문에 없는 행동 {index}",
            actor=replace(fact.actor, surface="원문에 없는 인물")
            if fact.actor is not None
            else None,
        )
        for index, fact in enumerate(case.expected.facts)
    )
    candidate = replace(case.expected, facts=facts)
    before = candidate
    observed = observe_sources(case, snapshot, candidate, model_input)
    assert observed["checks"]["source.quote_exact"]["mismatches"] == 0
    assert observed["checks"]["fact.action_in_source"]["mismatches"] == len(facts)
    assert observed["checks"]["fact.role_surface_in_source"]["mismatches"] == sum(
        fact.actor is not None for fact in facts
    )
    assert observed["candidate_assessment"]["assessment_availability"] == (
        "unvalidated_candidate"
    )
    assert "rules_accepted" not in observed["candidate_assessment"]
    assert observed["candidate_validated"] is False
    assert all(
        finding["semantic_verdict"] == "human_review_pending"
        for finding in observed["findings"]
    )
    assert candidate == before


def test_failed_candidates_keep_observation_and_quality_comparison(tmp_path):
    case = fixture()
    budget = CallBudget(32)
    expected = EvaluationModel(case, None, budget)

    def generate(model_input):
        candidate = expected._generate(model_input)
        return replace(
            candidate,
            facts=tuple(
                replace(fact, action="검사에 없는 행동") for fact in candidate.facts
            ),
        )

    recorder = AttemptRecorder(
        tmp_path,
        case,
        ModelLimits("gpt-6-luna", 10, 120000, 16384, 136384),
        False,
        observe_sources=True,
    )
    result, official = run_case(
        case, assemble(case, Mock(generate=generate), budget, recorder)
    )
    assert result.outcome == Outcome.FAILED
    assert not result.document_proposals
    assert official["omission_metrics"] == "not_evaluated"
    assert len(recorder.records) == 3  # first failed call and its two repairs
    for record in recorder.records:
        observed = record["source_observation"]
        assert observed["checks"]["fact.action_in_source"]["mismatches"] > 0
        assert observed["candidate_assessment"]["omission_metrics"] == "evaluated"
        assert record["validation"]["outcome"] == "FAILED"
    summary = source_summary(
        [{"case": case.name, "observed_attempts": recorder.records}]
    )
    assert summary["groups"]["initial"]["candidates_with_mismatches"] == 1
    assert summary["groups"]["repair"]["candidates_with_mismatches"] == 2


def test_observation_does_not_overwrite_last_call_with_aggregate_candidate(tmp_path):
    case = fixture()
    recorder = AttemptRecorder(
        tmp_path,
        case,
        ModelLimits("gpt-6-luna", 10, 120000, 16384, 136384),
        False,
        observe_sources=True,
    )
    result, _ = run_case(case, assemble(case, None, CallBudget(32), recorder))
    assert result.outcome == Outcome.PROPOSED
    assert len(recorder.records) > 1
    for record in recorder.records:
        observed = record["source_observation"]
        assert observed["fact_count"] == len(record["candidate"]["facts"])


def test_quote_integrity_is_observed_without_replacing_the_quote():
    case = fixture()
    snapshot = evaluation_snapshot(case)
    model_input = RefreshRules().model_inputs(snapshot, RefreshRules().chunk(snapshot))[
        0
    ]
    fact = case.expected.facts[0]
    fact = replace(fact, evidence=(replace(fact.evidence[0], quote="잘못된 인용문"),))
    candidate = replace(case.expected, facts=(fact,))
    observation = observe_sources(case, snapshot, candidate, model_input)
    assert observation["checks"]["source.quote_exact"]["mismatches"] == 1
    assert observation["findings"][0]["hint"] == "source_integrity_mismatch"
    assert candidate.facts[0].evidence[0].quote == "잘못된 인용문"


def test_unparseable_response_is_unavailable_instead_of_zero_mismatches(tmp_path):
    case, scenario, recorder = setup(tmp_path, "{invalid")
    recorder.observe_sources = True
    result, _ = run_case(case, scenario)
    assert result.outcome == Outcome.FAILED
    summary = source_summary(
        [{"case": case.name, "observed_attempts": recorder.records}]
    )
    initial = summary["groups"]["initial"]
    assert initial["attempts"] == initial["unavailable_candidates"] == 1
    assert initial["observed_candidates"] == 0
    assert initial["checks"]["fact.action_in_source"]["checked"] == 0


def test_cli_observation_artifacts_are_separate_and_re_renderable(tmp_path):
    from evaluation.markdown import write_report

    directory = tmp_path / "observed"
    assert (
        main(
            arguments(directory)
            + ["--suite", "semantic", "--max-calls", "16", "--observe-sources"]
        )
        == 0
    )
    summary = json.loads((directory / "source-observation.json").read_text())
    assert summary["strict_validation_unchanged"]
    assert summary["groups"]["initial"]["observed_candidates"] == 16
    assert summary["groups"]["repair"]["attempts"] == 0
    assert summary["groups"]["initial"]["candidates_with_mismatches"] == 0
    write_report(directory)
    assert (directory / "source-observation.md").is_file()
    assert (directory / "source-observation.json").stat().st_mode & 0o777 == 0o600
