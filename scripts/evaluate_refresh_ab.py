"""Run isolated prompt trials, or compare saved reports without model calls."""

import argparse
import io
import json
import time
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path

from app.adapters.llm import ModelLimits
from app.refresh.errors import RefreshFailure
from evaluation.artifacts import write_json
from evaluation.comparison import (
    COMMON_FAILURES,
    EXPERIMENT_VERSION,
    comparison_from_directory,
    report_failure,
    write_comparison,
)
from evaluation.runner import planned_calls
from scripts import evaluate_refresh


def suite_cases(suite, model, trial):
    """Reuse the existing CLI's datasets; conditions never select different cases."""
    factory = {
        "basic": evaluate_refresh.cases,
        "novel": lambda model, trial: (evaluate_refresh.novel_case(model, trial),),
        "novel-settings": lambda model, trial: (
            evaluate_refresh.novel_settings_case(model, trial),
        ),
        "new-settings": evaluate_refresh.new_setting_cases,
        "semantic": evaluate_refresh.semantic_cases,
    }.get(suite)
    if suite == "boundary":
        factory = getattr(evaluate_refresh, "boundary_cases", None)
    if factory is None:
        raise ValueError("Evaluation suite is not available")
    return factory(model, trial)


def _option(args, environment, name, key, cast, default=None):
    value = getattr(args, name)
    if value is None:
        value = environment.get(key, default)
    if value is None:
        raise ValueError(f"Required setting: {key}")
    return cast(value)


def plan(args):
    """Freeze all limits before IO and reserve every child call budget in full."""
    if args.repeats < 2:
        raise ValueError("--repeats must be at least 2")
    if not args.trial or not args.trial.strip():
        raise ValueError("--trial is required for a fresh experiment")
    if args.overlap_chars <= 0:
        raise ValueError("--overlap-chars must be positive")
    if args.chunk_chars is not None and args.chunk_chars <= 0:
        raise ValueError("--chunk-chars must be positive")
    environment = evaluate_refresh.environment(args.env_file)
    limits = ModelLimits(
        _option(args, environment, "model", "OPENAI_MODEL", str),
        _option(args, environment, "timeout", "OPENAI_TIMEOUT_SECONDS", float),
        _option(args, environment, "max_input_tokens", "OPENAI_MAX_INPUT_TOKENS", int),
        _option(
            args, environment, "max_output_tokens", "OPENAI_MAX_OUTPUT_TOKENS", int
        ),
        _option(args, environment, "context_window", "OPENAI_CONTEXT_WINDOW", int),
    )
    limits.validate()
    maximum = _option(args, environment, "max_calls", "OPENAI_MAX_CALLS", int, 9)
    if maximum <= 0:
        raise ValueError("--max-calls must be positive")
    fixtures = tuple(suite_cases(args.suite, limits.model, args.trial))
    if args.selected:
        if not set(args.selected).issubset({case.name for case in fixtures}):
            raise ValueError("Unknown evaluation case")
        fixtures = tuple(case for case in fixtures if case.name in args.selected)
    if not fixtures:
        raise ValueError("At least one case is required")
    arms = [
        {"name": "A", "prompt_version": "refresh-prompt-v11", "chunk_overlap_chars": 0},
        {"name": "B", "prompt_version": "refresh-prompt-v12", "chunk_overlap_chars": 0},
    ]
    if args.include_overlap:
        arms.append(
            {
                "name": "C",
                "prompt_version": "refresh-prompt-v12",
                "chunk_overlap_chars": args.overlap_chars,
            }
        )
    for arm in arms:
        altered = []
        for case in fixtures:
            changes = {}
            if args.chunk_chars is not None:
                changes["chunk_chars"] = args.chunk_chars
            if arm["chunk_overlap_chars"]:
                changes["chunk_overlap_chars"] = arm["chunk_overlap_chars"]
            chunk_chars = changes.get("chunk_chars", case.request.settings.chunk_chars)
            if arm["chunk_overlap_chars"] >= chunk_chars:
                raise ValueError("Chunk overlap must be smaller than chunk size")
            request = replace(
                case.request,
                settings=replace(case.request.settings, **changes),
                model_settings=replace(
                    case.request.model_settings, prompt_version=arm["prompt_version"]
                ),
            )
            altered.append(replace(case, request=request))
        if sum(planned_calls(case) for case in altered) > maximum:
            raise ValueError("Child call budget is smaller than the selected suite")
    reserved = args.repeats * len(arms) * maximum
    if args.live and args.max_total_calls is None:
        raise ValueError("--max-total-calls is required for a live experiment")
    ceiling = args.max_total_calls if args.max_total_calls is not None else reserved
    if ceiling <= 0 or reserved > ceiling:
        raise ValueError("Sum of child call budgets exceeds --max-total-calls")
    if args.live and not environment.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required for live evaluation")
    runs = []
    for index in range(args.repeats):
        shift = index % len(arms)
        order = arms[shift:] + arms[:shift]
        for arm in order:
            runs.append(
                {
                    "arm": arm["name"],
                    "repeat": index + 1,
                    "trial": f"{args.trial}-{arm['name'].lower()}-r{index + 1:02d}",
                    "directory": f"runs/r{index + 1:02d}-{arm['name'].lower()}",
                    "status": "scheduled",
                }
            )
    return {
        "schema_version": EXPERIMENT_VERSION,
        "live": args.live,
        "suite": args.suite,
        "repeats": args.repeats,
        "arms": arms,
        "cases": [case.name for case in fixtures],
        "common": {
            "limits": asdict(limits),
            "max_calls": maximum,
            "chunk_chars": args.chunk_chars,
            "reasoning": "model_default",
        },
        "max_total_calls": ceiling,
        "reserved_calls": reserved,
        "abort_reason": None,
        "runs": runs,
    }


def child_arguments(args, manifest, run):
    arm = next(arm for arm in manifest["arms"] if arm["name"] == run["arm"])
    limits = manifest["common"]["limits"]
    arguments = [
        "--suite",
        args.suite,
        "--model",
        limits["model"],
        "--timeout",
        str(limits["timeout_seconds"]),
        "--max-input-tokens",
        str(limits["max_input_tokens"]),
        "--max-output-tokens",
        str(limits["max_output_tokens"]),
        "--context-window",
        str(limits["context_window"]),
        "--max-calls",
        str(manifest["common"]["max_calls"]),
        "--trial",
        run["trial"],
        "--output",
        str(args.output / run["directory"]),
        "--prompt-version",
        arm["prompt_version"],
        "--chunk-overlap-chars",
        str(arm["chunk_overlap_chars"]),
    ]
    if args.env_file:
        arguments += ["--env-file", str(args.env_file)]
    if args.live:
        arguments.append("--live")
    if args.chunk_chars is not None:
        arguments += ["--chunk-chars", str(args.chunk_chars)]
    for name in manifest["cases"]:
        arguments += ["--case", name]
    return arguments


def _shared_failure(directory):
    for path in directory.glob("*.json"):
        if "-attempt-" in path.stem:
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        code = report_failure(report)
        if code in COMMON_FAILURES:
            return code
    return None


def comparison_exit_code(comparison):
    if not comparison["comparability"]["comparable"] or any(
        summary["evaluation_outcomes"].get(key, 0)
        for summary in comparison["arms"].values()
        for key in ("failed", "execution_failed")
    ):
        return 1
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--suite",
        choices=(
            "basic",
            "novel",
            "novel-settings",
            "new-settings",
            "semantic",
            "boundary",
        ),
        default="novel-settings",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-total-calls", type=int)
    parser.add_argument("--trial")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="selected")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--include-overlap", action="store_true")
    parser.add_argument("--overlap-chars", type=int, default=200)
    parser.add_argument("--chunk-chars", type=int)
    args = parser.parse_args(argv)
    try:
        if args.reports_dir:
            if args.live:
                raise ValueError("Saved-report comparison cannot use --live")
            comparison = comparison_from_directory(args.reports_dir)
            args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
            write_comparison(args.output, comparison)
            print(
                json.dumps(
                    {
                        "decision": comparison["decision"]["status"],
                        "read_only_replay": True,
                        "output": str(args.output),
                    }
                )
            )
            return comparison_exit_code(comparison)
        manifest = plan(args)
        args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        write_json(args.output / "experiment.json", manifest)
    except (OSError, ValueError, RefreshFailure) as error:
        parser.error(str(error))
    failures = False
    for run in manifest["runs"]:
        run["status"] = "running"
        write_json(args.output / "experiment.json", manifest)
        started = time.perf_counter()
        try:
            with redirect_stdout(io.StringIO()):
                code = evaluate_refresh.main(child_arguments(args, manifest, run))
            run["exit_code"] = code
            run["status"] = "completed"
            failures |= code == 1
            shared_failure = _shared_failure(args.output / run["directory"])
            if shared_failure:
                manifest["abort_reason"] = shared_failure
        except (Exception, SystemExit) as error:
            # Keep only the type; exception messages may contain provider data.
            run["status"] = "interrupted"
            run["error_type"] = type(error).__name__
            manifest["abort_reason"] = "child_execution_interrupted"
            failures = True
        finally:
            run["elapsed_seconds"] = time.perf_counter() - started
            write_json(args.output / "experiment.json", manifest)
        if manifest["abort_reason"]:
            break
    comparison = comparison_from_directory(args.output)
    write_comparison(args.output, comparison)
    print(
        json.dumps(
            {
                "live": args.live,
                "decision": comparison["decision"]["status"],
                "comparability": comparison["comparability"]["comparable"],
                "completed_runs": sum(
                    run["status"] == "completed" for run in manifest["runs"]
                ),
                "reserved_calls": manifest["reserved_calls"],
                "abort_reason": manifest["abort_reason"],
                "output": str(args.output),
            }
        )
    )
    return 1 if failures else comparison_exit_code(comparison)


if __name__ == "__main__":
    raise SystemExit(main())
