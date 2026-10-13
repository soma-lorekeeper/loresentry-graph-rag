"""Saved comparisons preserve missing/unknown states and summary privacy."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from evaluation.comparison import (
    EXPERIMENT_VERSION,
    comparison_from_directory,
    comparison_markdown,
    summarize_report,
    validate_manifest,
    write_comparison,
)

LIMITS = {
    "model": "gpt-6-luna",
    "timeout_seconds": 10,
    "max_input_tokens": 120000,
    "max_output_tokens": 8192,
    "context_window": 128192,
}
PRIVATE_TEXT = "PRIVATE_PROPOSAL_BODY"
PRIVATE_ID = "PRIVATE_FACT_OR_SOURCE_ID"


def experiment_manifest():
    return {
        "schema_version": EXPERIMENT_VERSION,
        "live": True,
        "suite": "novel-settings",
        "repeats": 2,
        "arms": [
            {
                "name": "A",
                "prompt_version": "refresh-prompt-v11",
                "chunk_overlap_chars": 0,
            },
            {
                "name": "B",
                "prompt_version": "refresh-prompt-v12",
                "chunk_overlap_chars": 0,
            },
        ],
        "cases": ["novel-settings"],
        "common": {"limits": LIMITS, "max_calls": 4},
        "max_total_calls": 16,
        "reserved_calls": 16,
        "abort_reason": None,
        "runs": [
            {
                "arm": arm,
                "repeat": repeat,
                "trial": f"trial-{arm}-{repeat}",
                "directory": f"runs/{repeat}-{arm}",
                "status": "completed",
            }
            for repeat, arm in ((1, "A"), (1, "B"), (2, "B"), (2, "A"))
        ],
    }


def sample_report(run, arm, *, live=True, outcome="PROPOSED"):
    return {
        "case": "novel-settings",
        "trial": run["trial"],
        "live": live,
        "response_source": "openai-api" if live else "expected-fixture",
        "settings": deepcopy(LIMITS),
        "evaluation_call_budget": 4,
        "evaluation": {
            "dataset_fingerprint": "dataset-hash",
            "criteria_fingerprint": "criteria-hash",
        },
        "request": {
            "job": {
                "request_id": run["trial"],
                "input_fingerprint": "condition-specific-hash",
            },
            "input_ref": {"key": run["trial"]},
            "seed_ids": [PRIVATE_ID],
            "target_ids": [PRIVATE_ID],
            "model_settings": {
                "model": "gpt-6-luna",
                "schema_version": "refresh-candidate-v5",
                "prompt_version": arm["prompt_version"],
            },
            "settings": {
                "max_model_calls": 20,
                "max_context_chars": 240000,
                "chunk_chars": 1000,
                "chunk_overlap_chars": arm["chunk_overlap_chars"],
            },
        },
        "assessment": {
            "missing_targets": [PRIVATE_ID],
            "missing_new_settings": [],
            "missing_relations": [PRIVATE_ID],
            "semantic_claim_mismatches": [],
            "unreviewed_semantic_claims": [],
            "semantic_assessment": {
                "structured_criteria": "matched",
                "source_entailment": "human_review_pending",
                "free_text_bodies": "human_review_pending",
            },
            "body_assessment": {
                "registered_patterns": "matched_patterns",
                "free_text_bodies": "human_review_pending",
            },
        },
        "result": {
            "outcome": outcome,
            "error": {"code": "INVALID_FACT", "message": PRIVATE_TEXT}
            if outcome == "FAILED"
            else None,
            "document_proposals": [{"value": PRIVATE_TEXT}]
            if outcome != "FAILED"
            else [],
            "new_document_proposals": [],
            "relation_proposals": [{"description": PRIVATE_TEXT}]
            if outcome != "FAILED"
            else [],
            "facts": [{"fact_id": PRIVATE_ID, "statement": PRIVATE_TEXT}],
            "evidence": [{"quote": PRIVATE_TEXT, "document_id": PRIVATE_ID}],
            "execution": {
                "candidate_schema_version": "refresh-candidate-v5",
                "usage": {"calls": 3, "input_tokens": 40, "output_tokens": 60},
                "attempts": [
                    {
                        "call_index": 1,
                        "attempt_index": 0,
                        "outcome": "FAILED",
                        "diagnostics": [
                            {"path": PRIVATE_ID, "offending_value": PRIVATE_TEXT}
                        ],
                    },
                    {"call_index": 1, "attempt_index": 1, "outcome": "PROPOSED"},
                    {"call_index": 2, "attempt_index": 0, "outcome": outcome},
                ],
            },
        },
    }


def sample_records():
    return [
        {
            "remote_requested": True,
            "usage_known": True,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "elapsed_seconds": 1.5,
        },
        {
            "remote_requested": True,
            "usage_known": False,
            "usage": None,
            "elapsed_seconds": 2.5,
        },
        {
            "remote_requested": True,
            "usage_known": True,
            "usage": {"input_tokens": 30, "output_tokens": 40},
            "elapsed_seconds": 3,
        },
    ]


def save_experiment(directory: Path, *, manifest=None):
    manifest = experiment_manifest() if manifest is None else manifest
    directory.mkdir()
    (directory / "experiment.json").write_text(json.dumps(manifest))
    arms = {arm["name"]: arm for arm in manifest["arms"]}
    for run in manifest["runs"]:
        target = directory / run["directory"]
        target.mkdir(parents=True)
        report = sample_report(run, arms[run["arm"]], live=manifest["live"])
        (target / "novel-settings.json").write_text(json.dumps(report))
        for index, record in enumerate(sample_records(), 1):
            (target / f"novel-settings-attempt-{index}.json").write_text(
                json.dumps(record)
            )
    return manifest


def test_failed_final_omissions_are_unevaluated_not_zero():
    manifest = experiment_manifest()
    report = sample_report(manifest["runs"][0], manifest["arms"][0], outcome="FAILED")
    report["assessment"].update(
        assessment_availability="unavailable_final_failure",
        omission_metrics="not_evaluated",
        semantic_claim_mismatches=[PRIVATE_ID],
    )
    row = summarize_report(report, sample_records())
    assert row["final_outcome"] == "FAILED"
    assert all(
        value == {"status": "not_evaluated", "count": None}
        for value in row["omissions"].values()
    )
    assert row["semantic"]["status"] == "not_evaluated"
    assert row["semantic"]["registered_errors"] is None
    assert row["final_proposals"] == {
        "document_proposals": 0,
        "new_document_proposals": 0,
        "relation_proposals": 0,
    }
    assert row["latency"]["scope"] == "failed_partial_execution"


def test_attempts_known_usage_and_latency_are_not_final_semantic_success():
    manifest = experiment_manifest()
    row = summarize_report(
        sample_report(manifest["runs"][0], manifest["arms"][0]), sample_records()
    )
    assert row["initial_validation"] == {
        "attempts": 2,
        "accepted": 1,
        "failed": 1,
        "unknown": 0,
        "validation_failed": 1,
        "model_failed": 0,
        "unknown_failed": 0,
    }
    assert row["repairs"] == {
        "attempts": 1,
        "accepted": 1,
        "failed": 0,
        "unknown": 0,
        "validation_failed": 0,
        "model_failed": 0,
        "unknown_failed": 0,
    }
    assert row["usage"]["input_tokens"] == {
        "total": None,
        "known_total": 40,
        "unknown": 1,
    }
    assert row["usage"]["output_tokens"] == {
        "total": None,
        "known_total": 60,
        "unknown": 1,
    }
    assert row["latency"] == {
        "total": 7.0,
        "known_total": 7.0,
        "unknown": 0,
        "scope": "successful_execution",
    }
    assert row["semantic"]["status"] == "needs_review"


def test_offline_usage_and_missing_records_stay_unknown():
    manifest = experiment_manifest()
    report = sample_report(manifest["runs"][0], manifest["arms"][0], live=False)
    row = summarize_report(report, sample_records())
    assert row["mode"] == "offline"
    assert row["usage"]["input_tokens"] == {
        "total": None,
        "known_total": None,
        "unknown": 3,
    }
    absent = summarize_report(report, [])
    assert absent["usage"]["input_tokens"]["total"] is None
    assert absent["latency"]["total"] is None


def test_comparison_is_read_only_private_and_inconclusive(tmp_path):
    directory = tmp_path / "saved"
    save_experiment(directory)
    before = {
        path: path.read_bytes() for path in directory.rglob("*") if path.is_file()
    }
    comparison = comparison_from_directory(directory)
    assert comparison["comparability"] == {"comparable": True, "errors": []}
    assert comparison["decision"]["status"] == "inconclusive"
    assert comparison["arms"]["A"]["repair_successes"] == 2
    assert comparison["arms"]["B"]["omissions"]["missing_targets"]["missing_count"] == 2
    markdown = comparison_markdown(comparison)
    assert "40 + 미확인" in markdown and "검토 대기" in markdown
    assert "완료 실행" in markdown
    assert PRIVATE_TEXT not in markdown and PRIVATE_ID not in markdown
    assert "diagnostics" not in markdown and "quote" not in markdown
    assert PRIVATE_TEXT not in json.dumps(comparison)
    assert PRIVATE_ID not in json.dumps(comparison)
    assert before == {
        path: path.read_bytes() for path in directory.rglob("*") if path.is_file()
    }
    output = tmp_path / "summary"
    output.mkdir()
    write_comparison(output, comparison)
    assert (output / "comparison.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_comparison(output, comparison)


def test_automatic_evaluation_failure_is_visible_despite_proposed_review_pending(
    tmp_path,
):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    for run in manifest["runs"]:
        path = directory / run["directory"] / "novel-settings.json"
        report = json.loads(path.read_text())
        report["assessment"].update(
            missing_targets=[],
            missing_relations=[],
            required_content_preserved=True,
        )
        if run["arm"] == "A":
            report["assessment"]["unreviewed_body_claims"] = [PRIVATE_TEXT]
            if run["repeat"] == 1:
                report["assessment"]["required_content_preserved"] = False
        elif run["repeat"] == 2:
            report["result"]["outcome"] = "FAILED"
            report["result"]["error"] = {
                "code": "INVALID_FACT",
                "message": PRIVATE_TEXT,
            }
        path.write_text(json.dumps(report))

    comparison = comparison_from_directory(directory)
    assert comparison["arms"]["A"]["outcomes"] == {"PROPOSED": 2}
    assert comparison["arms"]["A"]["evaluation_outcomes"] == {
        "failed": 1,
        "review_required": 1,
    }
    assert comparison["arms"]["B"]["evaluation_outcomes"] == {
        "passed": 1,
        "execution_failed": 1,
    }
    failed_trial = comparison["trials"][0]
    assert failed_trial["final_outcome"] == "PROPOSED"
    assert failed_trial["evaluation_outcome"] == "failed"
    assert failed_trial["semantic"]["status"] == "needs_review"

    markdown = comparison_markdown(comparison)
    assert "| 최종 결과 | 자동 평가 판정 |" in markdown
    assert r"| A | 1 | novel\-settings | PROPOSED | 실패 |" in markdown
    assert r"| A | 2 | novel\-settings | PROPOSED | 검토 대기 |" in markdown
    assert r"| B | 1 | novel\-settings | PROPOSED | 등록 검사 통과 |" in markdown
    assert r"| B | 2 | novel\-settings | FAILED | 실행 실패 |" in markdown
    assert (
        "| 자동 평가 실패 | 실행 실패 | 검토 대기 | 등록 검사 통과 | 미평가 |"
        in markdown
    )
    assert "| A | 2 / 2 | 1 | 0 | 1 | 0 | 0 |" in markdown
    assert "| B | 2 / 2 | 0 | 1 | 0 | 1 | 0 |" in markdown
    assert PRIVATE_TEXT not in markdown and PRIVATE_ID not in markdown
    assert comparison["decision"]["status"] == "inconclusive"


def test_unassessed_trial_remains_visible_in_automatic_evaluation_summary(tmp_path):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    path = directory / manifest["runs"][0]["directory"] / "novel-settings.json"
    report = json.loads(path.read_text())
    report["assessment"] = {}
    path.write_text(json.dumps(report))

    comparison = comparison_from_directory(directory)
    assert comparison["arms"]["A"]["evaluation_outcomes"] == {
        "unassessed": 1,
        "failed": 1,
    }
    markdown = comparison_markdown(comparison)
    assert r"| A | 1 | novel\-settings | PROPOSED | 미평가 |" in markdown
    assert "| A | 2 / 2 | 1 | 0 | 0 | 0 | 1 |" in markdown


def test_model_incomplete_and_domain_validation_failures_are_counted_separately(
    tmp_path,
):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    attempts = [
        {
            "call_index": 1,
            "attempt_index": 0,
            "outcome": "FAILED",
            "failure": {"code": "MODEL_INCOMPLETE", "message": PRIVATE_TEXT},
        },
        {
            "call_index": 2,
            "attempt_index": 0,
            "outcome": "FAILED",
            "failure": {"code": "INVALID_FACT", "message": PRIVATE_TEXT},
        },
        {"call_index": 3, "attempt_index": 0, "outcome": "FAILED"},
    ]
    for run in manifest["runs"]:
        path = directory / run["directory"] / "novel-settings.json"
        report = json.loads(path.read_text())
        report["result"]["execution"]["attempts"] = attempts
        path.write_text(json.dumps(report))

    comparison = comparison_from_directory(directory)
    assert comparison["trials"][0]["initial_validation"] == {
        "attempts": 3,
        "accepted": 0,
        "failed": 3,
        "unknown": 0,
        "validation_failed": 1,
        "model_failed": 1,
        "unknown_failed": 1,
    }
    assert comparison["arms"]["A"]["initial_validation_failures"] == 2
    assert comparison["arms"]["A"]["initial_model_failures"] == 2
    assert comparison["arms"]["A"]["initial_unknown_failures"] == 2
    assert comparison["arms"]["A"]["initial_failures"] == 6
    markdown = comparison_markdown(comparison)
    assert "| 최초 실패 / 시도 |" in markdown
    assert "| 최초 검증 실패 / 시도 |" not in markdown
    assert "| 최초 검증 / 모델 / 유형 미확인 실패 |" in markdown
    assert " / 2 / 2 |" in markdown
    assert PRIVATE_TEXT not in markdown


@pytest.mark.parametrize("observed", ["diagnostics", "candidate", "proposal_count"])
def test_failed_candidate_observations_identify_validation_failure(observed):
    manifest = experiment_manifest()
    report = sample_report(manifest["runs"][0], manifest["arms"][0])
    attempt = {
        "call_index": 1,
        "attempt_index": 1,
        "outcome": "FAILED",
        "failure": {"code": "MODEL_INVALID_RESPONSE"},
    }
    record = {}
    if observed == "diagnostics":
        attempt["diagnostics"] = [{"path": PRIVATE_ID}]
    elif observed == "candidate":
        record["candidate"] = {"value": PRIVATE_TEXT}
    else:
        attempt["document_proposals"] = 0
    report["result"]["execution"]["attempts"] = [attempt]
    row = summarize_report(report, [record])
    assert row["repairs"]["failed"] == 1
    assert row["repairs"]["validation_failed"] == 1
    assert row["repairs"]["model_failed"] == row["repairs"]["unknown_failed"] == 0


@pytest.mark.parametrize(
    "defect",
    ["dataset", "criteria", "timeout", "schema", "selection", "scope", "call_budget"],
)
def test_different_input_versions_or_limits_make_comparison_inconclusive(
    tmp_path, defect
):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    path = directory / manifest["runs"][-1]["directory"] / "novel-settings.json"
    report = json.loads(path.read_text())
    if defect in {"dataset", "criteria"}:
        report["evaluation"][f"{defect}_fingerprint"] = "changed-hash"
    elif defect == "timeout":
        report["settings"]["timeout_seconds"] = 99
    elif defect == "schema":
        report["request"]["model_settings"]["schema_version"] = "refresh-candidate-v4"
    elif defect == "selection":
        report["request"]["settings"]["max_context_chars"] = 10000
    elif defect == "scope":
        report["request"]["target_ids"] = []
    else:
        report["evaluation_call_budget"] = 9
    path.write_text(json.dumps(report))
    comparison = comparison_from_directory(directory)
    assert not comparison["comparability"]["comparable"]
    assert comparison["decision"]["status"] == "inconclusive"


def test_missing_evaluation_metadata_does_not_guess_from_request_fingerprint(tmp_path):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    path = directory / manifest["runs"][0]["directory"] / "novel-settings.json"
    report = json.loads(path.read_text())
    del report["evaluation"]
    path.write_text(json.dumps(report))
    comparison = comparison_from_directory(directory)
    assert not comparison["comparability"]["comparable"]
    assert any(
        error["reason"] == "missing_comparison_metadata"
        for error in comparison["comparability"]["errors"]
    )


@pytest.mark.parametrize("duplicated", ["trial", "directory"])
def test_trial_reuse_is_not_independent_generation(duplicated):
    manifest = experiment_manifest()
    manifest["runs"][1][duplicated] = manifest["runs"][0][duplicated]
    with pytest.raises(ValueError, match="fresh trial"):
        validate_manifest(manifest)


def test_failed_run_cannot_win_by_short_latency_or_zero_final_proposals(tmp_path):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    for run in manifest["runs"]:
        if run["arm"] == "B":
            path = directory / run["directory"] / "novel-settings.json"
            report = sample_report(run, manifest["arms"][1], outcome="FAILED")
            path.write_text(json.dumps(report))
    comparison = comparison_from_directory(directory)
    omissions = comparison["arms"]["B"]["omissions"]["missing_targets"]
    assert omissions["not_evaluated_trials"] == 2
    assert omissions["evaluated_trials"] == 0
    assert omissions["missing_count"] is None
    assert comparison["decision"]["status"] == "inconclusive"
    markdown = comparison_markdown(comparison)
    assert "실패까지 일부 실행" in markdown and "미평가 / 미평가 / 미평가" in markdown


def test_missing_case_report_is_partial_experiment(tmp_path):
    directory = tmp_path / "saved"
    manifest = save_experiment(directory)
    (directory / manifest["runs"][-1]["directory"] / "novel-settings.json").unlink()
    comparison = comparison_from_directory(directory)
    assert not comparison["comparability"]["comparable"]
    assert comparison["trials"][-1]["status"] == "not_run"
    assert "미실행" in comparison_markdown(comparison)
