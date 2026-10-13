"""A/B orchestration reserves budgets and never reuses a generation request."""

import argparse
import json
from pathlib import Path

import pytest

from scripts import evaluate_refresh_ab as cli
from tests.refresh.test_comparison import (
    LIMITS,
    sample_records,
    sample_report,
    save_experiment,
)


def arguments(output):
    return [
        "--suite",
        "basic",
        "--case",
        "document",
        "--model",
        "gpt-6-luna",
        "--timeout",
        "10",
        "--max-input-tokens",
        "120000",
        "--max-output-tokens",
        "8192",
        "--context-window",
        "128192",
        "--max-calls",
        "4",
        "--trial",
        "independent",
        "--output",
        str(output),
    ]


def install_fake_main(monkeypatch, *, failure=None, evaluation_failure=False):
    calls = []

    def fake_main(argv):
        parser = argparse.ArgumentParser()
        for flag in (
            "suite",
            "model",
            "timeout",
            "max-input-tokens",
            "max-output-tokens",
            "context-window",
            "max-calls",
            "trial",
            "output",
            "prompt-version",
            "chunk-overlap-chars",
            "chunk-chars",
            "env-file",
        ):
            parser.add_argument(f"--{flag}")
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--case", action="append", dest="selected")
        options = parser.parse_args(argv)
        calls.append(options)
        target = Path(options.output)
        target.mkdir(parents=True, exist_ok=False)
        arm = {
            "prompt_version": options.prompt_version,
            "chunk_overlap_chars": int(options.chunk_overlap_chars),
        }
        for name in options.selected:
            report = sample_report({"trial": options.trial}, arm, live=options.live)
            report["assessment"].update(
                missing_targets=[],
                missing_relations=[],
                required_content_preserved=not evaluation_failure,
            )
            report["case"] = name
            report["settings"] = {
                "model": options.model,
                "timeout_seconds": float(options.timeout),
                "max_input_tokens": int(options.max_input_tokens),
                "max_output_tokens": int(options.max_output_tokens),
                "context_window": int(options.context_window),
            }
            report["evaluation_call_budget"] = int(options.max_calls)
            report["request"]["settings"]["chunk_chars"] = int(
                options.chunk_chars or 1000
            )
            if failure:
                report["result"]["outcome"] = "FAILED"
                report["result"]["error"] = {
                    "code": failure,
                    "message": "private provider detail",
                }
            (target / f"{name}.json").write_text(json.dumps(report))
            for index, record in enumerate(sample_records(), 1):
                (target / f"{name}-attempt-{index}.json").write_text(json.dumps(record))
        print("child summary is suppressed")
        return 1 if failure else 0

    monkeypatch.setattr(cli.evaluate_refresh, "main", fake_main)
    return calls


def test_offline_repetitions_alternate_order_and_keep_limits_and_outputs_isolated(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "experiment"
    calls = install_fake_main(monkeypatch)
    assert cli.main(arguments(output)) == 2
    manifest = json.loads((output / "experiment.json").read_text())
    assert [run["arm"] for run in manifest["runs"]] == ["A", "B", "B", "A"]
    assert len({call.trial for call in calls}) == 4
    assert len({call.output for call in calls}) == 4
    assert all(not call.live for call in calls)
    assert {call.prompt_version for call in calls} == {
        "refresh-prompt-v11",
        "refresh-prompt-v12",
    }
    assert all(call.chunk_overlap_chars == "0" for call in calls)
    assert all(call.selected == ["document"] for call in calls)
    assert all(
        call.model == LIMITS["model"]
        and call.max_calls == "4"
        and call.timeout == "10.0"
        for call in calls
    )
    assert manifest["common"]["limits"] == LIMITS
    assert manifest["reserved_calls"] == manifest["max_total_calls"] == 16
    comparison = json.loads((output / "comparison.json").read_text())
    assert comparison["comparability"]["comparable"]
    assert comparison["decision"]["status"] == "inconclusive"
    assert "오프라인 고정 기준선" in (output / "comparison.md").read_text()
    assert "child summary" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(arguments(output))
    assert len(calls) == 4


def test_overlap_is_a_separate_opt_in_arm_with_same_chunk_size_and_budget(
    tmp_path, monkeypatch
):
    output = tmp_path / "experiment"
    calls = install_fake_main(monkeypatch)
    assert (
        cli.main(
            arguments(output)
            + [
                "--include-overlap",
                "--chunk-chars",
                "900",
                "--overlap-chars",
                "200",
                "--max-total-calls",
                "24",
            ]
        )
        == 2
    )
    manifest = json.loads((output / "experiment.json").read_text())
    assert [run["arm"] for run in manifest["runs"]] == ["A", "B", "C", "B", "C", "A"]
    assert manifest["reserved_calls"] == 24
    assert all(call.chunk_chars == "900" and call.max_calls == "4" for call in calls)
    overlap = [call for call in calls if call.chunk_overlap_chars == "200"]
    assert len(overlap) == 2
    assert all(call.prompt_version == "refresh-prompt-v12" for call in overlap)
    assert json.loads((output / "comparison.json").read_text())["comparability"][
        "comparable"
    ]


@pytest.mark.parametrize(
    "extra",
    [
        ["--repeats", "1"],
        ["--max-total-calls", "15"],
        ["--include-overlap", "--chunk-chars", "200"],
        ["--case", "unknown-case"],
    ],
)
def test_invalid_experiment_is_rejected_before_output_or_model_calls(
    tmp_path, monkeypatch, extra
):
    output = tmp_path / "invalid"
    calls = install_fake_main(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(arguments(output) + extra)
    assert not output.exists()
    assert not calls


def test_live_requires_explicit_whole_experiment_budget_before_child_calls(
    tmp_path, monkeypatch
):
    output = tmp_path / "invalid"
    calls = install_fake_main(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-not-used")
    with pytest.raises(SystemExit):
        cli.main(arguments(output) + ["--live"])
    assert not output.exists()
    assert not calls


@pytest.mark.parametrize("failure", ["MODEL_AUTH_FAILED", "MODEL_QUOTA_EXHAUSTED"])
def test_shared_auth_or_quota_failure_aborts_remaining_arms(
    tmp_path, monkeypatch, failure
):
    output = tmp_path / "experiment"
    calls = install_fake_main(monkeypatch, failure=failure)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-not-used")
    assert cli.main(arguments(output) + ["--live", "--max-total-calls", "16"]) == 1
    assert len(calls) == 1
    manifest = json.loads((output / "experiment.json").read_text())
    assert manifest["abort_reason"] == failure
    assert sum(run["status"] == "scheduled" for run in manifest["runs"]) == 3
    comparison = json.loads((output / "comparison.json").read_text())
    assert comparison["decision"]["status"] == "inconclusive"
    assert sum(row["status"] == "not_run" for row in comparison["trials"]) == 3
    assert "private provider detail" not in (output / "comparison.md").read_text()


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [("passed", 2), ("review_required", 2), ("failed", 1), ("execution_failed", 1)],
)
def test_saved_report_replay_never_invokes_main_or_changes_original_files(
    tmp_path, monkeypatch, outcome, expected_code
):
    saved = tmp_path / "saved"
    manifest = save_experiment(saved)
    for run in manifest["runs"]:
        path = saved / run["directory"] / "novel-settings.json"
        report = json.loads(path.read_text())
        report["assessment"].update(
            missing_targets=[],
            missing_relations=[],
            required_content_preserved=outcome != "failed",
        )
        if outcome == "review_required":
            report["assessment"]["unreviewed_body_claims"] = ["private claim"]
        elif outcome == "execution_failed":
            report["result"]["outcome"] = "FAILED"
            report["result"]["error"] = {
                "code": "MODEL_INCOMPLETE",
                "message": "private model detail",
            }
        path.write_text(json.dumps(report))
    before = {path: path.read_bytes() for path in saved.rglob("*") if path.is_file()}

    def unexpected_call(argv):
        raise AssertionError("Saved-report comparison must not invoke generation")

    monkeypatch.setattr(cli.evaluate_refresh, "main", unexpected_call)
    output = tmp_path / "replay"
    assert (
        cli.main(["--reports-dir", str(saved), "--output", str(output)])
        == expected_code
    )
    assert (output / "comparison.json").exists()
    assert not (output / "experiment.json").exists()
    assert before == {
        path: path.read_bytes() for path in saved.rglob("*") if path.is_file()
    }
    with pytest.raises(SystemExit):
        cli.main(
            ["--reports-dir", str(saved), "--output", str(tmp_path / "other"), "--live"]
        )


def test_failed_automatic_assessment_returns_one_even_if_child_claims_success(
    tmp_path, monkeypatch
):
    output = tmp_path / "experiment"
    calls = install_fake_main(monkeypatch, evaluation_failure=True)
    assert cli.main(arguments(output)) == 1
    assert len(calls) == 4
    comparison = json.loads((output / "comparison.json").read_text())
    assert comparison["comparability"]["comparable"]
    assert comparison["arms"]["A"]["evaluation_outcomes"] == {"failed": 2}
    assert comparison["decision"]["status"] == "inconclusive"


def test_env_file_is_forwarded_but_model_and_limits_are_frozen_without_secrets(
    tmp_path, monkeypatch
):
    output = tmp_path / "experiment"
    environment = tmp_path / "settings.env"
    environment.write_text(
        "OPENAI_API_KEY=PRIVATE_KEY_SENTINEL\nOPENAI_MODEL=gpt-5.6-luna\n"
    )
    calls = install_fake_main(monkeypatch)
    assert cli.main(arguments(output) + ["--env-file", str(environment)]) == 2
    assert all(
        call.env_file == str(environment) and call.model == "gpt-6-luna"
        for call in calls
    )
    for filename in ("experiment.json", "comparison.json", "comparison.md"):
        assert "PRIVATE_KEY_SENTINEL" not in (output / filename).read_text()
