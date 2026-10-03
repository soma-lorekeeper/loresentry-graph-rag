"""실행: .venv/bin/python -m scripts.evaluate_refresh --help. 기본은 오프라인이다."""

import argparse
import json
import os
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from openai import OpenAI

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.refresh.errors import RefreshFailure
from app.refresh.serialization import result_to_payload
from evaluation.artifacts import AttemptRecorder, RecordingClient, write_json
from evaluation.cases import cases
from evaluation.runner import CallBudget, assemble, run_case


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
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="selected")
    parser.add_argument("--repeat-saved", action="store_true")
    args = parser.parse_args(argv)
    try:
        env = environment(args.env_file)

        def option(name, key, cast):
            value = getattr(args, name)
            if value is None:
                value = env.get(key)
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
        budget = CallBudget(option("max_calls", "OPENAI_MAX_CALLS", int))
        fixtures = cases(limits.model, args.trial)
        if args.selected:
            if not set(args.selected) <= {c.name for c in fixtures}:
                raise ValueError("Unknown evaluation case")
            fixtures = tuple(c for c in fixtures if c.name in args.selected)
        required_calls = sum(max(1, len(c.targets)) for c in fixtures)
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
            recorder = AttemptRecorder(args.output, case, limits, args.live)
            model = (
                OpenAIProposalModel(RecordingClient(client, recorder), limits)
                if client is not None
                else None
            )
            scenario = assemble(case, model, budget, recorder)
            report = {
                "case": case.name,
                "live": args.live,
                "trial": args.trial,
                "request": asdict(case.request),
                "settings": asdict(limits),
            }
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
                report["attempts"] = len(recorder.records)
                report["observed_usage"] = [r["usage"] for r in recorder.records]
                report["snapshot"] = (
                    asdict(snapshot)
                    if (snapshot := scenario.artifacts.read_context(case.request))
                    else None
                )
                write_json(args.output / f"{case.name}.json", report)
            reports.append(report)
        failed = any(
            r.get("failure") or not r["assessment"]["rules_accepted"] for r in reports
        )
    print(
        json.dumps(
            {
                "live": args.live,
                "cases": len(reports),
                "calls": budget.used,
                "failed": failed,
                "output": str(args.output),
            }
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
