"""Compare independent saved refresh trials without exposing proposal text."""

import json
import math
import os
from collections import Counter
from pathlib import Path

from evaluation.cases import evaluation_outcome
from evaluation.markdown import inline

EXPERIMENT_VERSION = "refresh-ab-experiment-v1"
COMPARISON_VERSION = "refresh-ab-comparison-v1"
PROPOSALS = (
    "document_proposals",
    "new_document_proposals",
    "relation_proposals",
)
OMISSIONS = ("missing_targets", "missing_new_settings", "missing_relations")
COMMON_FAILURES = {"MODEL_AUTH_FAILED", "MODEL_QUOTA_EXHAUSTED"}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _seconds(value):
    return (
        float(value)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def _observed_sum(values):
    known = [value for value in values if value is not None]
    return {
        "total": sum(known) if len(known) == len(values) else None,
        "known_total": sum(known) if known else None,
        "unknown": len(values) - len(known),
    }


def report_failure(report):
    """Only a failure code is needed; provider messages stay in the raw report."""
    return (report.get("failure") or report.get("result", {}).get("error") or {}).get(
        "code"
    )


def _failure_kind(validation, record):
    if validation.get("outcome") != "FAILED":
        return None
    failure = validation.get("failure") or record.get("failure") or {}
    code = failure.get("code", "")
    domain_failure = isinstance(code, str) and (
        code.startswith(("INVALID", "CONFLICTING", "UNKNOWN"))
        or code in {"REVISION_MISMATCH", "DUPLICATE_SETTING"}
    )
    if (
        domain_failure
        or validation.get("diagnostics")
        or record.get("candidate") is not None
        or any(_count(validation.get(key)) is not None for key in PROPOSALS)
    ):
        return "validation_failed"
    if isinstance(code, str) and code.startswith("MODEL_"):
        return "model_failed"
    return "unknown_failed"


def summarize_report(report, records):
    """Keep failed omissions unevaluated and unknown usage distinct from zero."""
    result = report.get("result") or {}
    assessment = report.get("assessment") or {}
    execution = result.get("execution") or {}
    tracked = execution.get("attempts") or []
    if not tracked:
        tracked = [record.get("validation") or {} for record in records]
    attempts = []
    for index in range(max(len(tracked), len(records))):
        record = records[index] if index < len(records) else {}
        validation = tracked[index] if index < len(tracked) else {}
        attempts.append(
            {
                "call_index": validation.get("call_index", record.get("call_index")),
                "attempt_index": validation.get(
                    "attempt_index", record.get("attempt_index")
                ),
                "outcome": validation.get("outcome"),
                "failure_kind": _failure_kind(validation, record),
            }
        )

    def phase(repair):
        selected = [
            attempt
            for attempt in attempts
            if type(attempt["attempt_index"]) is int
            and (attempt["attempt_index"] > 0) == repair
        ]
        accepted = sum(
            attempt["outcome"] in {"PROPOSED", "NO_CHANGE"} for attempt in selected
        )
        failed = sum(attempt["outcome"] == "FAILED" for attempt in selected)
        return {
            "attempts": len(selected),
            "accepted": accepted,
            "failed": failed,
            "unknown": len(selected) - accepted - failed,
            **{
                kind: sum(attempt["failure_kind"] == kind for attempt in selected)
                for kind in ("validation_failed", "model_failed", "unknown_failed")
            },
        }

    final_outcome = result.get("outcome") or "NO_RESULT"
    evaluated = final_outcome in {"PROPOSED", "NO_CHANGE"} and not report_failure(
        report
    )
    omissions = {
        key: {
            "status": (
                "not_evaluated"
                if not evaluated
                else "evaluated"
                if isinstance(assessment.get(key), list)
                else "unknown"
            ),
            "count": (
                len(assessment[key])
                if evaluated and isinstance(assessment.get(key), list)
                else None
            ),
        }
        for key in OMISSIONS
    }
    semantic = assessment.get("semantic_assessment") or {}
    mismatches = assessment.get("semantic_claim_mismatches")
    body_mismatches = assessment.get("body_claim_mismatches")
    registered_errors = sum(
        len(value) for value in (mismatches, body_mismatches) if isinstance(value, list)
    )
    pending_checks = sum(
        len(assessment[key])
        for key in (
            "unreviewed_semantic_claims",
            "unreviewed_body_claims",
            "unreviewed_new_settings",
            "unreviewed_relations",
        )
        if isinstance(assessment.get(key), list)
    )
    human_pending = (
        any(
            semantic.get(key) == "human_review_pending"
            for key in ("source_entailment", "free_text_bodies")
        )
        or assessment.get("semantic_invention_review") == "manual_review_required"
    )
    semantic_state = (
        "not_evaluated"
        if not evaluated
        else "registered_error"
        if registered_errors
        else "needs_review"
        if human_pending or pending_checks
        else "unknown"
    )

    live = report.get("live") is True
    usages = []
    times = []
    remote_calls = 0
    for index in range(max(len(records), len(attempts))):
        record = records[index] if index < len(records) else {}
        usage = record.get("usage")
        known = (
            live
            and record.get("usage_known") is True
            and isinstance(usage, dict)
            and _count(usage.get("input_tokens")) is not None
            and _count(usage.get("output_tokens")) is not None
        )
        usages.append(usage if known else None)
        times.append(_seconds(record.get("elapsed_seconds")))
        remote_calls += record.get("remote_requested") is True
    input_usage = _observed_sum(
        [usage["input_tokens"] if usage is not None else None for usage in usages]
    )
    output_usage = _observed_sum(
        [usage["output_tokens"] if usage is not None else None for usage in usages]
    )
    latency = _observed_sum(times)
    if not records:
        input_usage = output_usage = latency = {
            "total": None,
            "known_total": None,
            "unknown": max(1, len(attempts)),
        }
    return {
        "mode": "live" if live else "offline",
        "response_source": report.get("response_source", "unknown"),
        "final_outcome": final_outcome,
        "evaluation_outcome": evaluation_outcome(report),
        "failure_code": report_failure(report),
        "initial_validation": phase(False),
        "repairs": phase(True),
        "unknown_phase_attempts": sum(
            type(attempt["attempt_index"]) is not int for attempt in attempts
        ),
        "omissions": omissions,
        "semantic": {
            "status": semantic_state,
            "registered_errors": registered_errors if evaluated else None,
            "pending_checks": pending_checks if evaluated else None,
            "human_review_pending": human_pending if evaluated else None,
        },
        "usage": {
            "recorded_calls": _count((execution.get("usage") or {}).get("calls")),
            "remote_requested_calls": remote_calls if records else None,
            "input_tokens": input_usage,
            "output_tokens": output_usage,
        },
        "latency": {
            **latency,
            "scope": (
                "successful_execution" if evaluated else "failed_partial_execution"
            ),
        },
        "final_proposals": {
            key: len(result[key]) if isinstance(result.get(key), list) else None
            for key in PROPOSALS
        },
    }


def _child(root, relative):
    path = Path(relative)
    resolved = (root / path).resolve()
    if (
        path.is_absolute()
        or not resolved.is_relative_to(root.resolve())
        or resolved == root.resolve()
    ):
        raise ValueError("Experiment report paths must stay inside the experiment")
    return resolved


def validate_manifest(manifest):
    """Every arm/repeat has its own trial and output; metadata never authorizes IO."""
    if manifest.get("schema_version") != EXPERIMENT_VERSION:
        raise ValueError("Unsupported experiment manifest")
    repeats = manifest.get("repeats")
    arms = manifest.get("arms")
    runs = manifest.get("runs")
    names = manifest.get("cases")
    if type(repeats) is not int or repeats < 2:
        raise ValueError("An experiment requires at least two repetitions")
    if not isinstance(arms, list) or len(arms) < 2 or not isinstance(runs, list):
        raise ValueError("An experiment requires independent arms and runs")
    arm_names = [arm.get("name") for arm in arms]
    if any(not isinstance(name, str) or not name for name in arm_names) or len(
        set(arm_names)
    ) != len(arm_names):
        raise ValueError("Arm names must be nonempty and unique")
    if (
        not isinstance(names, list)
        or not names
        or any(
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            for name in names
        )
        or len(set(names)) != len(names)
    ):
        raise ValueError("Case names must be nonempty and unique")
    keys, trials, directories = set(), set(), set()
    for run in runs:
        key = (run.get("arm"), run.get("repeat"))
        trial, directory = run.get("trial"), run.get("directory")
        if (
            key[0] not in arm_names
            or type(key[1]) is not int
            or not 1 <= key[1] <= repeats
            or key in keys
            or not isinstance(trial, str)
            or not trial
            or trial in trials
            or not isinstance(directory, str)
            or not directory
            or directory in directories
        ):
            raise ValueError("Each arm/repeat needs a fresh trial and directory")
        keys.add(key)
        trials.add(trial)
        directories.add(directory)
    if len(keys) != repeats * len(arms):
        raise ValueError("Experiment manifest is missing an arm/repeat")


def _signature(report):
    evaluation = report.get("evaluation") or {}
    request = report.get("request") or {}
    model = request.get("model_settings") or {}
    limits = report.get("settings") or {}
    if (
        not all(
            isinstance(evaluation.get(key), str) and evaluation[key]
            for key in ("dataset_fingerprint", "criteria_fingerprint")
        )
        or not all(
            key in limits
            for key in (
                "model",
                "timeout_seconds",
                "max_input_tokens",
                "max_output_tokens",
                "context_window",
            )
        )
        or not model.get("schema_version")
    ):
        return None
    return {
        "evaluation": evaluation,
        "model_limits": limits,
        "model_settings": {
            key: value for key, value in model.items() if key != "prompt_version"
        },
        "candidate_schema_version": (
            (report.get("result") or {}).get("execution") or {}
        ).get("candidate_schema_version", model["schema_version"]),
        "evaluation_call_budget": report.get("evaluation_call_budget"),
        "selection_settings": {
            key: value
            for key, value in (request.get("settings") or {}).items()
            if key != "chunk_overlap_chars"
        },
        "request_scope": {
            key: value
            for key, value in request.items()
            if key not in {"job", "input_ref", "settings", "model_settings"}
        },
    }


def comparison_from_directory(directory: Path):
    """Read an existing experiment. This function never writes or invokes a model."""
    manifest = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    arms = {arm["name"]: arm for arm in manifest["arms"]}
    references = {}
    rows, errors = [], []
    for run in manifest["runs"]:
        path = _child(directory, run["directory"])
        for name in manifest["cases"]:
            row = {"arm": run["arm"], "repeat": run["repeat"], "case": name}
            report_path = path / f"{name}.json"
            if not report_path.exists():
                row.update(status="not_run", final_outcome="NOT_RUN")
                errors.append({**row, "reason": "missing_case_report"})
                rows.append(row)
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            records = []
            for record_path in sorted(
                path.glob(f"{name}-attempt-*.json"),
                key=lambda p: int(p.stem.rsplit("-", 1)[1]),
            ):
                records.append(json.loads(record_path.read_text(encoding="utf-8")))
            row.update(status="recorded", **summarize_report(report, records))
            rows.append(row)
            arm = arms[run["arm"]]
            request = report.get("request") or {}
            if (
                report.get("case") != name
                or report.get("trial") != run["trial"]
                or report.get("live") is not manifest.get("live")
                or (request.get("model_settings") or {}).get("prompt_version")
                != arm.get("prompt_version")
                or (request.get("settings") or {}).get("chunk_overlap_chars", 0)
                != arm.get("chunk_overlap_chars")
                or report.get("settings") != manifest.get("common", {}).get("limits")
                or (request.get("model_settings") or {}).get("model")
                != (report.get("settings") or {}).get("model")
                or report.get("evaluation_call_budget")
                != manifest.get("common", {}).get("max_calls")
            ):
                errors.append(
                    {
                        "arm": run["arm"],
                        "repeat": run["repeat"],
                        "case": name,
                        "reason": "condition_metadata_mismatch",
                    }
                )
            signature = _signature(report)
            if signature is None:
                errors.append(
                    {
                        "arm": run["arm"],
                        "repeat": run["repeat"],
                        "case": name,
                        "reason": "missing_comparison_metadata",
                    }
                )
            elif name not in references:
                references[name] = signature
            elif signature != references[name]:
                errors.append(
                    {
                        "arm": run["arm"],
                        "repeat": run["repeat"],
                        "case": name,
                        "reason": "input_or_limits_mismatch",
                    }
                )

    summaries = {}
    for arm in arms:
        selected = [
            row for row in rows if row["arm"] == arm and row["status"] == "recorded"
        ]
        summaries[arm] = {
            "recorded_case_trials": len(selected),
            "planned_case_trials": manifest["repeats"] * len(manifest["cases"]),
            "outcomes": dict(Counter(row["final_outcome"] for row in selected)),
            "evaluation_outcomes": dict(
                Counter(row["evaluation_outcome"] for row in selected)
            ),
            "initial_validation_failures": sum(
                row["initial_validation"]["validation_failed"] for row in selected
            ),
            "initial_model_failures": sum(
                row["initial_validation"]["model_failed"] for row in selected
            ),
            "initial_unknown_failures": sum(
                row["initial_validation"]["unknown_failed"] for row in selected
            ),
            "initial_failures": sum(
                row["initial_validation"]["failed"] for row in selected
            ),
            "repair_attempts": sum(row["repairs"]["attempts"] for row in selected),
            "repair_successes": sum(row["repairs"]["accepted"] for row in selected),
            "omissions": {
                key: {
                    "evaluated_trials": sum(
                        row["omissions"][key]["status"] == "evaluated"
                        for row in selected
                    ),
                    "not_evaluated_trials": sum(
                        row["omissions"][key]["status"] == "not_evaluated"
                        for row in selected
                    ),
                    "unknown_trials": sum(
                        row["omissions"][key]["status"] == "unknown" for row in selected
                    ),
                    "missing_count": _observed_sum(
                        [
                            row["omissions"][key]["count"]
                            for row in selected
                            if row["omissions"][key]["status"] == "evaluated"
                        ]
                    )["total"]
                    if any(
                        row["omissions"][key]["status"] == "evaluated"
                        for row in selected
                    )
                    else None,
                }
                for key in OMISSIONS
            },
            "semantic_states": dict(
                Counter(row["semantic"]["status"] for row in selected)
            ),
        }
    reasons = ["human_semantic_review_required"]
    if not manifest.get("live"):
        reasons.append("offline_fixture_is_not_independent_model_generation")
    if errors:
        reasons.append("incomplete_or_incomparable_trials")
    return {
        "schema_version": COMPARISON_VERSION,
        "experiment": {
            "live": manifest.get("live") is True,
            "suite": manifest.get("suite"),
            "repeats": manifest["repeats"],
            "arms": manifest["arms"],
            "common": manifest.get("common", {}),
            "max_total_calls": manifest.get("max_total_calls"),
            "reserved_calls": manifest.get("reserved_calls"),
            "abort_reason": manifest.get("abort_reason"),
        },
        "comparability": {"comparable": not errors, "errors": errors},
        "decision": {"status": "inconclusive", "reasons": reasons},
        "arms": summaries,
        "trials": rows,
    }


def _observed(value):
    if value["total"] is not None:
        return str(round(value["total"], 3))
    known = value["known_total"]
    return f"{round(known, 3)} + 미확인" if known is not None else "미확인"


def _phase(value, counter):
    text = f"{value[counter]} / {value['attempts']}"
    return text + f" (미확인 {value['unknown']})" if value["unknown"] else text


def comparison_markdown(comparison):
    """Only counts and fixed status labels enter this human-facing summary."""
    experiment = comparison["experiment"]
    lines = [
        "# Refresh 반복 비교",
        "",
        f"실행: {'실제 모델' if experiment['live'] else '오프라인 고정 기준선'} · 반복: {experiment['repeats']}회",
        "",
        "판정: **결론 보류**. 등록 기준 검사는 원문 함의와 본문 의미 정확성의 사람 검토를 대신하지 않습니다.",
        "실패한 최종 결과의 누락은 미평가입니다. 실패 시까지 측정한 시간은 완료 실행 시간과 직접 비교할 수 없습니다.",
        "사용량 미확인은 0이 아닙니다. 오프라인 시도는 독립 모델 생성이나 실제 토큰 비용으로 해석하지 않습니다.",
        "",
        f"입력·설정 비교 가능: {'예' if comparison['comparability']['comparable'] else '아니오'}",
        "",
        "| 조건 | 프롬프트 | 청크 중첩 |",
        "| --- | --- | --- |",
    ]
    for arm in experiment["arms"]:
        lines.append(
            f"| {inline(arm['name'])} | {inline(arm['prompt_version'])} | {arm['chunk_overlap_chars']} |"
        )
    lines += [
        "",
        "| 조건 | 기록 / 예정 시행 | 자동 평가 실패 | 실행 실패 | 검토 대기 | 등록 검사 통과 | 미평가 | 최초 검증 / 모델 / 유형 미확인 실패 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in experiment["arms"]:
        summary = comparison["arms"][arm["name"]]
        outcomes = summary["evaluation_outcomes"]
        lines.append(
            f"| {inline(arm['name'])} | {summary['recorded_case_trials']} / {summary['planned_case_trials']}"
            + " | "
            + " | ".join(
                str(outcomes.get(key, 0))
                for key in (
                    "failed",
                    "execution_failed",
                    "review_required",
                    "passed",
                    "unassessed",
                )
            )
            + f" | {summary['initial_validation_failures']} / {summary['initial_model_failures']} / {summary['initial_unknown_failures']} |"
        )
    lines += [
        "",
        "| 조건 | 반복 | 사례 | 최종 결과 | 자동 평가 판정 | 최초 실패 / 시도 | 보완 성공 / 시도 | 필수 대상 / 신규 / 관계 누락 | 의미 상태 | 입력 / 출력 토큰 | 측정 시간(초) | 시간 범위 | 최종 수정 / 신규 / 관계 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    evaluation_labels = {
        "execution_failed": "실행 실패",
        "failed": "실패",
        "review_required": "검토 대기",
        "passed": "등록 검사 통과",
        "unassessed": "미평가",
    }
    semantic_labels = {
        "not_evaluated": "미평가",
        "registered_error": "등록 기준 불일치",
        "needs_review": "검토 대기",
        "unknown": "미확인",
    }
    for row in comparison["trials"]:
        prefix = f"| {inline(row['arm'])} | {row['repeat']} | {inline(row['case'])} | {inline(row['final_outcome'])}"
        if row["status"] != "recorded":
            lines.append(
                prefix
                + " | 미실행 | 미실행 | 미실행 | 미평가 | 미평가 | 미확인 | 미확인 | 미실행 | 미생성 |"
            )
            continue
        initial, repair = row["initial_validation"], row["repairs"]
        omissions = " / ".join(
            str(row["omissions"][key]["count"])
            if row["omissions"][key]["status"] == "evaluated"
            else "미평가"
            if row["omissions"][key]["status"] == "not_evaluated"
            else "미확인"
            for key in OMISSIONS
        )
        usage = " / ".join(
            _observed(row["usage"][key]) for key in ("input_tokens", "output_tokens")
        )
        proposals = " / ".join(
            str(row["final_proposals"][key])
            if row["final_proposals"][key] is not None
            else "미확인"
            for key in PROPOSALS
        )
        scope = (
            "완료 실행"
            if row["latency"]["scope"] == "successful_execution"
            else "실패까지 일부 실행"
        )
        lines.append(
            prefix
            + f" | {evaluation_labels[row['evaluation_outcome']]} | {_phase(initial, 'failed')} | {_phase(repair, 'accepted')} | {omissions} | {semantic_labels[row['semantic']['status']]} | {usage} | {_observed(row['latency'])} | {scope} | {proposals} |"
        )
    return "\n".join(lines) + "\n"


def write_comparison(directory: Path, comparison):
    """Write new private summary artifacts without replacing an existing summary."""
    for filename, content in (
        (
            "comparison.json",
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        ),
        ("comparison.md", comparison_markdown(comparison)),
    ):
        path = directory / filename
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
