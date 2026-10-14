"""실행: .venv/bin/python -m scripts.evaluate_refresh --help. 기본은 오프라인이다."""

import argparse
import json
import os
from contextlib import ExitStack
from dataclasses import asdict, fields, replace
from pathlib import Path

from openai import OpenAI

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.refresh.errors import RefreshFailure
from app.refresh.prompt_versions import SUPPORTED_PROMPT_VERSIONS
from app.refresh.serialization import result_to_payload
from evaluation.artifacts import AttemptRecorder, RecordingClient, write_json
from evaluation.cases import (
    cases,
    evaluation_fingerprint,
    evaluation_outcome,
    new_setting_cases,
)
from evaluation.confidence import write_confidence_report
from evaluation.markdown import write_report
from evaluation.novel import novel_case, novel_settings_case
from evaluation.repair_observation import write_repair_report
from evaluation.replay import ResponseReplay
from evaluation.runner import CallBudget, assemble, planned_calls, run_case
from evaluation.semantic_cases import (
    boundary_cases,
    rebase_expected_sources,
    semantic_cases,
)
from evaluation.source_observation import write_source_report


def environment(path: Path | None) -> dict:
    """로컬 key=value 파일을 읽고 프로세스 환경값을 우선한다. 값을 출력하지 않는다."""
    values = {}
    if path is not None:
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                raise ValueError("Invalid environment file")
            values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    return values


def main(argv=None) -> int:
    """명시적 live 플래그와 키·모델·예산이 있을 때만 원격 호출한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--responses-dir", type=Path)
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
        default="basic",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--prompt-version", choices=sorted(SUPPORTED_PROMPT_VERSIONS))
    parser.add_argument("--chunk-chars", type=int)
    parser.add_argument("--chunk-overlap-chars", type=int, default=0)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="selected")
    parser.add_argument("--repeat-saved", action="store_true")
    parser.add_argument(
        "--two-stage",
        action="store_true",
        help="Plan by paragraph IDs, then write and assemble every planned edit",
    )
    parser.add_argument(
        "--confidence",
        action="store_true",
        help="Request fact-level confidence with experimental v6 schema; observation only",
    )
    parser.add_argument(
        "--observe-sources",
        action="store_true",
        help="Record all source expression mismatches without relaxing strict results",
    )
    parser.add_argument(
        "--single-call",
        action="store_true",
        help="Evaluate all targets and new settings in one generation call",
    )
    args = parser.parse_args(argv)
    try:
        if args.two_stage and not (args.live or args.responses_dir):
            raise ValueError("Two-stage evaluation requires live or replayed responses")
        if args.confidence and not (args.live or args.responses_dir):
            raise ValueError(
                "Confidence evaluation requires live or replayed model responses"
            )
        env = environment(args.env_file)

        def option(name, key, cast, default=None):
            value = getattr(args, name)
            if value is None:
                value = env.get(key)
            if value is None:
                value = default
            if value is None:
                raise ValueError(f"Required setting: {key}")
            return cast(value)

        limits = ModelLimits(
            option("model", "OPENAI_MODEL", str),
            option("timeout", "OPENAI_TIMEOUT_SECONDS", float),
            option("max_input_tokens", "OPENAI_MAX_INPUT_TOKENS", int),
            option("max_output_tokens", "OPENAI_MAX_OUTPUT_TOKENS", int),
            option("context_window", "OPENAI_CONTEXT_WINDOW", int),
        )
        limits.validate()
        budget = CallBudget(option("max_calls", "OPENAI_MAX_CALLS", int, 18))
        fixtures = (
            (novel_case(limits.model, args.trial),)
            if args.suite == "novel"
            else (novel_settings_case(limits.model, args.trial),)
            if args.suite == "novel-settings"
            else new_setting_cases(limits.model, args.trial)
            if args.suite == "new-settings"
            else semantic_cases(limits.model, args.trial)
            if args.suite == "semantic"
            else boundary_cases(limits.model, args.trial)
            if args.suite == "boundary"
            else cases(limits.model, args.trial)
        )
        if args.selected:
            if not set(args.selected) <= {c.name for c in fixtures}:
                raise ValueError("Unknown evaluation case")
            fixtures = tuple(c for c in fixtures if c.name in args.selected)
        metadata = {
            case.name: {
                "dataset_fingerprint": evaluation_fingerprint(
                    {
                        "documents": case.source.documents,
                        "related": case.related,
                        "targets": case.targets,
                    }
                ),
                "criteria_fingerprint": evaluation_fingerprint(
                    {
                        field.name: getattr(case, field.name)
                        for field in fields(case)
                        if field.name
                        not in {"name", "request", "source", "related", "targets"}
                    }
                ),
            }
            for case in fixtures
        }
        configured = []
        for case in fixtures:
            settings = replace(
                case.request.settings,
                max_model_calls=min(
                    case.request.settings.max_model_calls, budget.maximum
                )
                if args.two_stage
                else case.request.settings.max_model_calls,
                chunk_chars=(
                    args.chunk_chars
                    if args.chunk_chars is not None
                    else case.request.settings.chunk_chars
                ),
                chunk_overlap_chars=args.chunk_overlap_chars,
                separate_discovery=(
                    False
                    if args.single_call or args.two_stage
                    else case.request.settings.separate_discovery
                ),
            )
            model_settings = replace(
                case.request.model_settings,
                prompt_version=(
                    (args.prompt_version or "refresh-prompt-v13")
                    if args.two_stage
                    else (
                        args.prompt_version
                        or case.request.model_settings.prompt_version
                    )
                ),
                schema_version=(
                    (
                        "refresh-candidate-v8"
                        if args.confidence
                        else "refresh-candidate-v7"
                    )
                    if args.two_stage
                    else "refresh-candidate-v6"
                    if args.confidence
                    else case.request.model_settings.schema_version
                ),
            )
            request = replace(
                case.request, settings=settings, model_settings=model_settings
            )
            case = replace(case, request=request)
            if not args.live and args.responses_dir is None:
                case = rebase_expected_sources(case)
            configured.append(case)
        fixtures = tuple(configured)
        required_calls = sum(
            planned_calls(case, single_call=args.single_call) for case in fixtures
        )
        if required_calls > budget.maximum:
            raise ValueError("Call budget smaller than selected evaluation suite")
        if args.live and not env.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is required for live evaluation")
        args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    except (ValueError, OSError, RefreshFailure) as error:
        # Only local validation messages, never the environment or provider body.
        parser.error(str(error))
    reports = []
    with ExitStack() as stack:
        client = None
        if args.live:
            client = stack.enter_context(
                OpenAI(
                    api_key=env["OPENAI_API_KEY"],
                    max_retries=0,
                    timeout=limits.timeout_seconds,
                )
            )
        for case in fixtures:
            recorder = AttemptRecorder(
                args.output,
                case,
                limits,
                args.live,
                observe_sources=args.observe_sources,
                observe_confidence=args.confidence,
            )
            model = (
                OpenAIProposalModel(RecordingClient(client, recorder), limits)
                if client is not None
                else None
            )
            if args.responses_dir is not None:
                model = ResponseReplay(args.responses_dir, case, recorder)
            source = (
                "openai-api"
                if args.live
                else "conversation-assistant"
                if args.responses_dir
                else "expected-fixture"
            )
            scenario = assemble(
                case,
                model,
                budget,
                recorder,
                **({"single_call": True} if args.single_call else {}),
            )
            report = {
                "case": case.name,
                "response_source": source,
                "live": args.live,
                "trial": args.trial,
                "request": asdict(case.request),
                "settings": asdict(limits),
                "evaluation": metadata[case.name],
                "evaluation_call_budget": budget.maximum,
                "call_strategy": "two-stage"
                if args.two_stage
                else "single-call"
                if args.single_call
                else "default",
                "confidence_mode": args.confidence,
            }
            result = None
            try:
                result, assessment = run_case(
                    case, scenario, repeat_saved=args.repeat_saved
                )
                report.update(
                    assessment=assessment,
                    result=result_to_payload(
                        case.request,
                        result,
                        scenario.artifacts.read_context(case.request),
                    ),
                )
            except RefreshFailure as error:
                report["failure"] = asdict(error.failure)
            except Exception as error:
                report["unexpected_error_type"] = type(error).__name__
                raise
            finally:
                if result is not None:
                    recorder.reconcile(result.attempts)
                report["attempts"] = len(recorder.records)
                report["observed_usage"] = [r["usage"] for r in recorder.records]
                report["observed_attempts"] = [
                    {
                        **{
                            key: record.get(key)
                            for key in (
                                "attempt",
                                "call_index",
                                "attempt_index",
                                "repair_version",
                                "stage",
                                "validation",
                                "instruction_fingerprint",
                            )
                        },
                        **(
                            {"source_observation": record["source_observation"]}
                            if "source_observation" in record
                            else {}
                        ),
                        **{
                            key: record[key]
                            for key in (
                                "repair_observation",
                                "raw_source_observation",
                                "confidence_observation",
                            )
                            if key in record
                        },
                    }
                    for record in recorder.records
                ]
                report["snapshot"] = (
                    asdict(snapshot)
                    if (snapshot := scenario.artifacts.read_context(case.request))
                    else None
                )
                write_json(args.output / f"{case.name}.json", report)
                write_report(args.output)
            reports.append(report)
            # Shared credentials/quota cannot be repaired by another fixture.
            failure = report.get("failure") or report.get("result", {}).get("error")
            if failure and failure.get("code") in {
                "MODEL_AUTH_FAILED",
                "MODEL_QUOTA_EXHAUSTED",
            }:
                break
        outcomes = [evaluation_outcome(report) for report in reports]
        failed = any(
            outcome in {"execution_failed", "failed", "unassessed"}
            for outcome in outcomes
        )
        review_required = "review_required" in outcomes
        if args.observe_sources:
            write_source_report(args.output, reports)
        write_repair_report(args.output, reports)
        if args.confidence:
            write_confidence_report(args.output, reports)
    print(
        json.dumps(
            {
                "live": args.live,
                "cases": len(reports),
                "calls": budget.used,
                "repair_attempts": sum(
                    attempt["attempt_index"] > 0
                    for report in reports
                    for attempt in report["observed_attempts"]
                ),
                "repair_successes": sum(
                    attempt["attempt_index"] > 0
                    and (attempt.get("validation") or {}).get("outcome")
                    in {"PROPOSED", "NO_CHANGE"}
                    for report in reports
                    for attempt in report["observed_attempts"]
                ),
                "failed": failed,
                "review_required": review_required,
                "output": str(args.output),
            }
        )
    )
    return 1 if failed else 2 if review_required else 0


if __name__ == "__main__":
    raise SystemExit(main())
