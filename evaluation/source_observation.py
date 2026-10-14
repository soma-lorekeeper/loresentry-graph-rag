"""평가 전용 출처 관찰. 원래 후보를 수정하거나 서비스 검증 결과를 완화하지 않는다."""

import json
from dataclasses import asdict
from pathlib import Path
from unicodedata import normalize

from app.refresh.models import Outcome, RefreshResult
from evaluation.artifacts import write_json
from evaluation.cases import assess
from evaluation.markdown import block, inline

OBSERVATION_VERSION = "refresh-source-observation-v1"
SOURCE_RULES = (
    "source.quote_exact",
    "fact.action_in_source",
    "fact.role_surface_in_source",
)


def normalized_expression(value):
    """표기 차이의 후보만 찾는다. 정규화한 문자열로 검증을 통과시키지 않는다."""
    return "".join(normalize("NFKC", value).split())


def observe_sources(case, snapshot, candidate, model_input):
    """모든 사실의 출처·표현을 수집하고 미검증 후보의 등록 품질 기준도 비교한다."""
    documents = {document.document_id: document for document in snapshot.documents}
    context = json.loads(model_input.prompt)
    markers = context.get("evidence", [])
    checks = {rule: {"checked": 0, "mismatches": 0} for rule in SOURCE_RULES}
    findings = []

    def check(rule, matches, *, fact, source, value, role=None):
        checks[rule]["checked"] += 1
        if matches:
            return
        checks[rule]["mismatches"] += 1
        selected = [
            item["marker"]
            for item in markers
            if all(
                item.get(key) == getattr(source, key)
                for key in ("document_id", "revision_no", "start", "end", "quote")
            )
        ]
        other = [
            item["marker"]
            for item in markers
            if item["marker"] not in selected and value in item.get("quote", "")
        ]
        hint = (
            "source_integrity_mismatch"
            if rule == "source.quote_exact"
            else "possible_formatting_difference"
            if normalized_expression(value)
            and normalized_expression(value) in normalized_expression(source.quote)
            else "expression_in_other_marker"
            if other
            else "expression_not_found_in_context"
        )
        findings.append(
            {
                "rule": rule,
                "fact_id": fact.fact_id,
                "role": role,
                "value": value,
                "statement": fact.statement,
                "source": asdict(source),
                "source_markers": selected,
                "other_markers_containing_expression": other,
                "hint": hint,
                "semantic_verdict": "human_review_pending",
            }
        )

    unavailable_sources = 0
    for fact in candidate.facts:
        for source in fact.evidence:
            document = documents.get(source.document_id)
            if (
                document is None
                or not isinstance(document.body_text, str)
                or not 0 <= source.start < source.end <= len(document.body_text)
            ):
                unavailable_sources += 1
            else:
                check(
                    "source.quote_exact",
                    source.quote == document.body_text[source.start : source.end],
                    fact=fact,
                    source=source,
                    value=source.quote,
                )
            check(
                "fact.action_in_source",
                fact.action in source.quote,
                fact=fact,
                source=source,
                value=fact.action,
            )
            for role in ("actor", "recipient", "object"):
                entity = getattr(fact, role)
                if entity is not None:
                    check(
                        "fact.role_surface_in_source",
                        entity.surface in source.quote,
                        fact=fact,
                        source=source,
                        value=entity.surface,
                        role=role,
                    )

    raw_result = RefreshResult(
        snapshot.request.job,
        Outcome.PROPOSED
        if (
            candidate.document_proposals
            or candidate.new_document_proposals
            or candidate.relation_proposals
        )
        else Outcome.NO_CHANGE,
        candidate.document_proposals,
        candidate.usage,
        relation_proposals=candidate.relation_proposals,
        new_document_proposals=candidate.new_document_proposals,
        facts=candidate.facts,
        prompt_version=model_input.prompt_version,
        candidate_schema_version=model_input.schema_version,
    )
    assessment = assess(case, raw_result)
    assessment.pop("rules_accepted", None)
    assessment["assessment_availability"] = "unvalidated_candidate"
    return {
        "version": OBSERVATION_VERSION,
        "status": "observed",
        "candidate_validated": False,
        "fact_count": len(candidate.facts),
        "unavailable_sources": unavailable_sources,
        "checks": checks,
        "findings": findings,
        "candidate_assessment": assessment,
    }


def source_summary(reports):
    """최초·보완의 검사 단위 분모와 불일치 후보 수를 따로 집계한다."""
    groups = {}
    for kind in ("initial", "repair"):
        groups[kind] = {
            "attempts": 0,
            "observed_candidates": 0,
            "unavailable_candidates": 0,
            "candidates_with_mismatches": 0,
            "checks": {
                rule: {"checked": 0, "mismatches": 0, "candidates_with_mismatches": 0}
                for rule in SOURCE_RULES
            },
        }
    details = []
    for report in reports:
        for attempt in report.get("observed_attempts", []):
            observation = attempt.get("source_observation")
            if observation is None or observation.get("status") == "not_applicable":
                continue
            group = groups["repair" if attempt["attempt_index"] else "initial"]
            group["attempts"] += 1
            observed = observation["status"] == "observed"
            group["observed_candidates"] += observed
            group["unavailable_candidates"] += not observed
            findings = observation.get("findings", [])
            group["candidates_with_mismatches"] += bool(findings)
            for rule, counts in observation.get("checks", {}).items():
                group["checks"][rule]["checked"] += counts["checked"]
                group["checks"][rule]["mismatches"] += counts["mismatches"]
                group["checks"][rule]["candidates_with_mismatches"] += bool(
                    counts["mismatches"]
                )
            details.append(
                {
                    "case": report["case"],
                    "attempt": attempt["attempt"],
                    "call_index": attempt["call_index"],
                    "attempt_index": attempt["attempt_index"],
                    "strict_validation": attempt.get("validation"),
                    "observation": observation,
                }
            )
    return {
        "version": OBSERVATION_VERSION,
        "strict_validation_unchanged": True,
        "semantic_verdict": "human_review_pending",
        "groups": groups,
        "attempts": details,
    }


def write_source_report(directory: Path, reports):
    """정식 결과와 분리한 관찰 JSON·Markdown을 비공개 평가 디렉터리에 쓴다."""
    summary = source_summary(reports)
    write_json(directory / "source-observation.json", summary)
    lines = [
        "# 출처 표현 관찰 결과",
        "",
        "관찰은 서비스 검증을 변경하지 않습니다. 원본과 유사도 보정 후 후보의 문자열 검사와",
        "등록 품질 기준 비교이며, 불일치 후보를 승인하거나 전체 의미를 검증한 결과가 아닙니다.",
        "",
        "| 단계 | 기록된 시도 | 관찰 후보 | 관찰 불가 | 불일치 후보 |",
        "|---|---:|---:|---:|---:|",
    ]
    for kind, group in summary["groups"].items():
        label = "최초" if kind == "initial" else "보완"
        lines.append(
            f"| {label} | {group['attempts']} | {group['observed_candidates']} | "
            f"{group['unavailable_candidates']} | {group['candidates_with_mismatches']} |"
        )
    lines += [
        "",
        "검사 분모는 호출 수가 아닌 실제로 검사한 원문·행동·nonnull 역할 표현 수입니다.",
        "",
        "| 단계 | 검사 | 검사 수 | 불일치 수 | 불일치 후보 수 |",
        "|---|---|---:|---:|---:|",
    ]
    for kind, group in summary["groups"].items():
        label = "최초" if kind == "initial" else "보완"
        for rule, counts in group["checks"].items():
            lines.append(
                f"| {label} | {inline(rule)} | {counts['checked']} | "
                f"{counts['mismatches']} | {counts['candidates_with_mismatches']} |"
            )
    for detail in summary["attempts"]:
        observation = detail["observation"]
        if not observation.get("findings"):
            continue
        lines += [
            "",
            f"## {inline(detail['case'])} / 시도 {detail['attempt']}",
            "",
            f"[원래 후보·정식 검증 기록]({detail['case']}-attempt-{detail['attempt']}.json)",
            "",
            "분류 힌트는 문자열 검색 결과이며 실제 표기 오류·잘못된 인물 연결 여부는 원문 검토가 필요합니다.",
        ]
        for finding in observation["findings"]:
            lines += [
                "",
                f"- 검사: {inline(finding['rule'])}; 역할: {inline(finding['role'])}",
                f"- 분류 힌트: {inline(finding['hint'])}",
                f"- 반환 표현: {inline(finding['value'])}",
                f"- 사실 문장: {inline(finding['statement'])}",
                "",
                block(finding["source"]["quote"]),
            ]
        lines += [
            "",
            "### 미검증 후보의 등록 품질 기준 비교",
            "",
            block(
                json.dumps(
                    observation["candidate_assessment"], ensure_ascii=False, indent=2
                ),
                "json",
            ),
        ]
    (directory / "source-observation.md").write_text("\n".join(lines) + "\n")
    (directory / "source-observation.md").chmod(0o600)
    return summary
