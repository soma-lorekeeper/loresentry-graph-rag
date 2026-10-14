"""실험 점수의 엄격한 전송·보존과 점수에 독립적인 기존 검증을 확인한다."""

import json
import re
from dataclasses import replace

import pytest

from app.adapters.llm import OpenAIProposalModel
from app.adapters.llm_schema import candidate_schema, parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import Outcome, Usage
from app.refresh.prompts import (
    CONFIDENCE_INSTRUCTION,
    IMPROVED_CONFIDENCE_INSTRUCTION,
    INSTRUCTION,
    build_prompt,
)
from app.refresh.rules import RefreshRules
from app.refresh.serialization import result_from_payload, result_to_payload
from evaluation.confidence import (
    observe_confidence,
    write_confidence_report,
)
from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_runner import arguments
from tests.refresh.test_llm import client_fixture, limits
from tests.refresh.test_semantic_relations import parse, semantic_fixture
from tests.refresh.test_validation_repair import (
    candidate_for,
    invalid_identity,
    repair_scenario,
    run_saved,
)


def fixture(score=0.8):
    payload, model_input, snapshot = semantic_fixture(
        schema_version="refresh-candidate-v6"
    )
    payload["facts"][0]["confidence"] = score
    return payload, model_input, snapshot


@pytest.mark.parametrize("score", [0, 0.01, 0.8, 1.0])
def test_score_is_recorded_without_filtering_low_confidence_facts(score):
    payload, model_input, snapshot = fixture(score)
    candidate = parse(payload, model_input)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.facts[0].confidence == score
    assert result.relation_proposals
    stored = result_to_payload(snapshot.request, result, snapshot)
    assert stored["facts"][0]["confidence"] == score
    assert (
        result_from_payload(json.loads(json.dumps(stored))).facts[0].confidence == score
    )


@pytest.mark.parametrize(
    "score", [-0.01, 1.01, None, True, "0.8", float("nan"), float("inf")]
)
def test_invalid_scores_fail_strict_parsing(score):
    payload, model_input, _ = fixture(score)
    with pytest.raises(RefreshFailure) as error:
        parse(payload, model_input)
    assert error.value.failure.code == "MODEL_INVALID_RESPONSE"
    assert error.value.diagnostics[0].path == "facts[0].confidence"


def test_score_is_required_only_in_v6_and_does_not_change_fact_identity():
    payload, model_input, _ = fixture()
    baseline_payload = json.loads(json.dumps(payload))
    baseline_payload["facts"][0].pop("confidence")
    baseline = parse(
        baseline_payload, replace(model_input, schema_version="refresh-candidate-v5")
    )
    scored = parse(payload, model_input)
    changed_score = parse(
        {**payload, "facts": [{**payload["facts"][0], "confidence": 0.1}]}, model_input
    )
    assert baseline.facts == scored.facts == changed_score.facts
    assert baseline.facts[0].confidence is None
    assert scored.facts[0].confidence == 0.8
    with pytest.raises(RefreshFailure):
        parse(baseline_payload, model_input)
    with pytest.raises(RefreshFailure):
        parse(payload, replace(model_input, schema_version="refresh-candidate-v5"))


def test_high_confidence_cannot_bypass_source_validation():
    payload, model_input, snapshot = fixture(1.0)
    payload["facts"][0]["action"] = "원문에 없는 행동"
    candidate = parse(payload, model_input)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_FACT"
    observation = observe_confidence(snapshot, candidate, result.diagnostics)
    assert observation["facts"][0]["confidence"] == 1
    assert observation["facts"][0]["mechanical_errors"] == ["fact.action_in_source"]


def test_summary_surface_difference_is_observed_without_being_counted_as_an_error():
    payload, model_input, snapshot = fixture()
    payload["facts"][0]["statement"] = "그는 백도윤에게 방문 전달을 맡겼다."
    candidate = parse(payload, model_input)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    observation = observe_confidence(snapshot, candidate, result.diagnostics)
    fact = observation["facts"][0]
    assert fact["mechanical_errors"] == []
    assert fact["summary_surface_differences"] == ["actor"]
    assert fact["strict_diagnostic_rules"] == []


def test_confidence_schema_reaches_sdk_and_response_parser():
    payload, model_input, _ = fixture(0.6)
    model_input = replace(
        model_input, model="gpt-6-luna", instructions=CONFIDENCE_INSTRUCTION
    )
    client = client_fixture()
    client.responses.create.return_value.output_text = json.dumps(payload)
    model_limits = replace(
        limits("gpt-6-luna"), max_input_tokens=80000, context_window=82000
    )
    candidate = OpenAIProposalModel(client, model_limits).generate(model_input)
    assert candidate.facts[0].confidence == 0.6
    schema = client.responses.create.call_args.kwargs["text"]["format"]["schema"]
    fact = schema["$defs"]["ConfidenceSourceFactDTO"]
    assert "confidence" in fact["required"]
    assert fact["properties"]["confidence"]["minimum"] == 0
    assert fact["properties"]["confidence"]["maximum"] == 1
    assert (
        "confidence"
        not in candidate_schema()["$defs"]["MarkerSourceFactDTO"]["properties"]
    )


@pytest.mark.parametrize(
    "instructions", [CONFIDENCE_INSTRUCTION, IMPROVED_CONFIDENCE_INSTRUCTION]
)
def test_confidence_examples_use_the_actual_strict_contract(instructions):
    blocks = re.findall(r"```json\n(.*?)\n```", instructions, re.DOTALL)
    assert len(blocks) == 4
    for index in (0, 2):
        context = json.loads(blocks[index])
        context.update(
            model_settings={
                "model": "gpt-6-luna",
                "prompt_version": "refresh-prompt-v11",
                "schema_version": "refresh-candidate-v6",
            },
            max_context_chars=480000,
        )
        model_input = build_prompt(json.dumps(context))
        candidate = parse_candidate(
            blocks[index + 1],
            Usage(),
            schema_version="refresh-candidate-v6",
            model_input=model_input,
        )
        assert all(fact.confidence == 0.8 for fact in candidate.facts)
    assert "### 사실별 자기평가 confidence" not in INSTRUCTION


def test_v6_repairs_keep_scores_in_feedback_and_do_not_use_them_as_a_threshold():
    scenario = repair_scenario()
    scenario.request = replace(
        scenario.request,
        model_settings=replace(
            scenario.request.model_settings, schema_version="refresh-candidate-v6"
        ),
    )
    scenario.snapshot = replace(scenario.snapshot, request=scenario.request)
    candidate = candidate_for(scenario)
    candidate = replace(
        candidate, facts=(replace(candidate.facts[0], confidence=0.01),)
    )
    scenario.model.responses.extend((invalid_identity(candidate), candidate))
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert result.usage.calls == 2
    assert result.facts[0].confidence == 0.01
    repair = json.loads(scenario.model.inputs[1].prompt)["validation_repair"]
    assert repair["previous_candidate"]["facts"][0]["confidence"] == 0.01


def test_old_results_omit_confidence_and_still_round_trip():
    payload, model_input, snapshot = semantic_fixture()
    result = RefreshRules().validate_candidate(
        snapshot, parse(payload, model_input), model_input
    )
    stored = result_to_payload(snapshot.request, result, snapshot)
    assert "confidence" not in stored["facts"][0]
    assert (
        result_from_payload(json.loads(json.dumps(stored))).facts[0].confidence is None
    )


def test_corrupt_stored_confidence_is_rejected():
    payload, model_input, snapshot = fixture()
    result = RefreshRules().validate_candidate(
        snapshot, parse(payload, model_input), model_input
    )
    stored = result_to_payload(snapshot.request, result, snapshot)
    stored["facts"][0]["confidence"] = 2.0
    with pytest.raises(ValueError, match="confidence"):
        result_from_payload(json.loads(json.dumps(stored)))


def test_summary_keeps_phase_candidate_and_missing_scores_separate(tmp_path):
    payload, model_input, snapshot = fixture(0.99)
    payload["facts"][0]["action"] = "없는 행동"
    observation = observe_confidence(snapshot, parse(payload, model_input))
    reports = [
        {
            "case": "confidence",
            "observed_attempts": [
                {
                    "attempt": 1,
                    "attempt_index": 0,
                    "confidence_observation": {
                        "raw": observation,
                        "effective": observation,
                    },
                },
                {
                    "attempt": 2,
                    "attempt_index": 1,
                    "confidence_observation": {
                        source: {"status": "unavailable"}
                        for source in ("raw", "effective")
                    },
                },
            ],
        }
    ]
    summary = write_confidence_report(tmp_path, reports)
    band = summary["groups"]["initial_raw"]["bands"][-1]
    assert band["facts"] == band["facts_with_mechanical_errors"] == 1
    assert band["mean_confidence"] == 0.99
    assert summary["groups"]["repair_raw"]["unavailable_attempts"] == 1
    assert summary["groups"]["repair_raw"]["bands"][-1]["mean_confidence"] is None
    assert "not_evaluated" in summary["semantic_calibration"]
    assert (tmp_path / "confidence-observation.json").stat().st_mode & 0o777 == 0o600


def test_cli_does_not_invent_confidence_from_expected_fixture(tmp_path):
    with pytest.raises(SystemExit):
        main(arguments(tmp_path / "run") + ["--confidence"])
    assert not (tmp_path / "run").exists()


def test_cli_replays_explicit_scores_and_writes_observation_without_network(tmp_path):
    from app.refresh.repair import build_repair_input
    from evaluation.runner import EvaluationRules, evaluation_snapshot
    from evaluation.semantic_cases import rebase_expected_sources, semantic_cases

    case = rebase_expected_sources(semantic_cases("gpt-6-luna", "test")[0])
    case = replace(
        case,
        request=replace(
            case.request,
            settings=replace(case.request.settings, separate_discovery=False),
            model_settings=replace(
                case.request.model_settings, schema_version="refresh-candidate-v6"
            ),
        ),
    )
    rules = EvaluationRules(None, None, single_call=True)
    snapshot = evaluation_snapshot(case)
    model_input = rules.model_inputs(snapshot, rules.chunk(snapshot))[0]
    wire = json.loads(
        build_repair_input(model_input, case.expected, (), 480000).prompt
    )["validation_repair"]["previous_candidate"]
    for fact in wire["facts"]:
        fact["confidence"] = (
            0.42  # 기능 테스트용 고정값이며 실제 모델의 confidence가 아니다.
        )
    replay = tmp_path / "responses"
    replay.mkdir()
    target = "+".join(model_input.target_ids) or "relations"
    envelope = {
        "input_fingerprint": case.request.job.input_fingerprint,
        "target_ids": list(model_input.target_ids),
        "prompt_version": model_input.prompt_version,
        "schema_version": "refresh-candidate-v6",
        "response_source": "conversation-assistant",
        "response_text": json.dumps(wire, ensure_ascii=False),
    }
    (replay / f"{case.name}--{target}.json").write_text(json.dumps(envelope))
    output = tmp_path / "run"
    status = main(
        arguments(output)
        + [
            "--model",
            "gpt-6-luna",
            "--suite",
            "semantic",
            "--case",
            case.name,
            "--single-call",
            "--responses-dir",
            str(replay),
            "--confidence",
            "--repeat-saved",
        ]
    )
    assert status in (0, 2)
    report = json.loads((output / f"{case.name}.json").read_text())
    assert report["confidence_mode"]
    assert (
        report["result"]["execution"]["candidate_schema_version"]
        == "refresh-candidate-v6"
    )
    assert report["result"]["facts"][0]["confidence"] == 0.42
    summary = json.loads((output / "confidence-observation.json").read_text())
    group = summary["groups"]["initial_raw"]
    assert group["bands"][0]["facts"] == len(wire["facts"])
    assert group["bands"][0]["mean_confidence"] == pytest.approx(0.42)
    assert len(report["observed_attempts"]) == 1
    attempt = json.loads((output / f"{case.name}-attempt-1.json").read_text())
    assert not attempt["remote_requested"]


def test_confidence_metadata_does_not_change_evaluation_criteria_fingerprint():
    from evaluation.cases import evaluation_fingerprint

    payload, model_input, _ = fixture()
    candidate = parse(payload, model_input)
    different = replace(candidate, facts=(replace(candidate.facts[0], confidence=0.1),))
    assert evaluation_fingerprint(candidate) == evaluation_fingerprint(different)
