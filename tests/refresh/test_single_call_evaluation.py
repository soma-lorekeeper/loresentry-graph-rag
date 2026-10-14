import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.refresh.models import Outcome
from app.refresh.rules import RefreshRules
from evaluation.novel import novel_settings_case
from evaluation.runner import (
    CallBudget,
    EvaluationModel,
    EvaluationRules,
    assemble,
    evaluation_snapshot,
    planned_calls,
    run_case,
)
from evaluation.semantic_cases import rebase_expected_sources, semantic_cases
from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_runner import arguments


def fixture():
    case = novel_settings_case("gpt-6-luna", "single-call")
    return replace(
        case,
        request=replace(
            case.request,
            settings=replace(case.request.settings, separate_discovery=False),
        ),
    )


def test_single_call_contains_both_full_chapters_and_all_eight_settings():
    case = fixture()
    snapshot = evaluation_snapshot(case)
    rules = EvaluationRules(None, None, single_call=True)
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert planned_calls(case, single_call=True) == len(inputs) == 1
    item = inputs[0]
    context = json.loads(item.prompt)
    assert set(item.target_ids) == {document.document_id for document in case.targets}
    assert len(item.target_ids) == 8
    assert len(context["documents"]) == 10
    assert item.allow_new_documents and item.allow_document_updates
    assert item.relation_scope.owner_target_ids == item.target_ids
    assert item.relation_scope.owns_fallback
    for document in case.source.documents:
        excerpts = [
            evidence
            for evidence in context["evidence"]
            if evidence["document_id"] == document.document_id
        ]
        assert "".join(evidence["quote"] for evidence in excerpts) == document.body_text
    # The production/default evaluation strategy continues to use eight calls.
    assert len(RefreshRules().model_inputs(snapshot, rules.chunk(snapshot))) == 8


def test_single_call_cli_accepts_three_call_ceiling_and_reuses_saved_result(tmp_path):
    directory = tmp_path / "single"
    outcome = main(
        arguments(directory)
        + [
            "--suite",
            "novel-settings",
            "--single-call",
            "--observe-sources",
            "--max-calls",
            "3",
            "--repeat-saved",
        ]
    )
    assert outcome in {0, 2}
    report = json.loads((directory / "novel-settings.json").read_text())
    assert report["call_strategy"] == "single-call"
    assert report["evaluation_call_budget"] == 3
    assert report["result"]["execution"]["usage"]["calls"] == 1
    assert len(list(directory.glob("*-attempt-*.json"))) == 1
    assert report["assessment"]["manuscript_content_preserved"]


def test_single_call_repair_uses_same_full_scope_and_stays_under_budget():
    case = rebase_expected_sources(
        semantic_cases("gpt-6-luna", "single-call-repair")[0]
    )
    budget = CallBudget(3)
    expected = EvaluationModel(case, None, budget)
    requests = []

    def generate(item):
        requests.append(item)
        candidate = expected._generate(item)
        if len(requests) == 1:
            return replace(
                candidate,
                facts=(replace(candidate.facts[0], action="원문에 없는 행동"),)
                + candidate.facts[1:],
            )
        return candidate

    result, _ = run_case(
        case,
        assemble(case, Mock(generate=generate), budget, single_call=True),
        repeat_saved=True,
    )
    assert result.outcome == Outcome.PROPOSED
    assert budget.used == len(requests) == 2
    assert set(requests[0].target_ids) == set(case.request.target_ids)
    assert requests[0].target_ids == requests[1].target_ids


def test_default_novel_cli_still_rejects_three_call_budget(tmp_path):
    directory = tmp_path / "default"
    with pytest.raises(SystemExit):
        main(arguments(directory) + ["--suite", "novel-settings", "--max-calls", "3"])
    assert not directory.exists()
