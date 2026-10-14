"""사실별 자기평가 점수와 관측 오류의 관계. 정답률 보정·자동 채택은 하지 않는다."""

from pathlib import Path

from app.refresh.facts import valid_confidence
from evaluation.artifacts import write_json
from evaluation.markdown import inline

CONFIDENCE_VERSION = "refresh-confidence-observation-v1"
BANDS = ((0, 0.5), (0.5, 0.8), (0.8, 0.95), (0.95, 1.0))


def observe_confidence(snapshot, candidate, diagnostics=None):
    """원문/행동/표면 검사와 진단을 점수에 연결하고 요약의 표현 차이는 따로 남긴다."""
    documents = {document.document_id: document for document in snapshot.documents}
    facts = []
    for fact in candidate.facts:
        errors = []
        summary_differences = []
        if len(fact.evidence) != 1:
            errors.append("source.single_marker")
        else:
            source = fact.evidence[0]
            document = documents.get(source.document_id)
            if (
                document is None
                or source.revision_no != document.revision_no
                or not 0 <= source.start < source.end <= len(document.body_text)
                or source.quote != document.body_text[source.start : source.end]
            ):
                errors.append("source.quote_exact")
            if fact.action not in source.quote:
                errors.append("fact.action_in_source")
            for role in ("actor", "recipient", "object"):
                entity = getattr(fact, role)
                if entity is None:
                    continue
                if entity.surface not in source.quote:
                    errors.append(f"{role}.surface_in_source")
                if entity.surface not in fact.statement:
                    summary_differences.append(role)
        facts.append(
            {
                "fact_id": fact.fact_id,
                "statement": fact.statement,
                "certainty": fact.certainty,
                "confidence": fact.confidence,
                "score_available": valid_confidence(fact.confidence),
                "mechanical_errors": errors,
                "summary_surface_differences": summary_differences,
                "strict_diagnostic_rules": (
                    [
                        diagnostic.rule
                        for diagnostic in diagnostics
                        if diagnostic.fact_id == fact.fact_id
                    ]
                    if diagnostics is not None
                    else None
                ),
                "semantic_verdict": "human_review_pending",
            }
        )
    return {
        "version": CONFIDENCE_VERSION,
        "status": "observed",
        "facts": facts,
        "semantic_calibration": "not_evaluated_without_fact_correctness_labels",
    }


def confidence_summary(reports):
    """최초·보완, 원본·서버 후보를 섞지 않고 점수 구간별 관측 오류를 센다."""
    groups = {}
    details = []
    for phase in ("initial", "repair"):
        for source in ("raw", "effective"):
            groups[f"{phase}_{source}"] = {
                "attempts": 0,
                "unavailable_attempts": 0,
                "facts_without_score": 0,
                "bands": [
                    {
                        "lower": lower,
                        "upper": upper,
                        "facts": 0,
                        "confidence_sum": 0.0,
                        "facts_with_mechanical_errors": 0,
                        "facts_with_strict_diagnostics": 0,
                        "strict_diagnostics_unavailable": 0,
                    }
                    for lower, upper in BANDS
                ],
            }
    for report in reports:
        for attempt in report.get("observed_attempts", []):
            observation = attempt.get("confidence_observation")
            if observation is None or all(
                value.get("status") == "not_applicable"
                for value in observation.values()
            ):
                continue
            phase = "repair" if attempt["attempt_index"] else "initial"
            details.append(
                {
                    "case": report["case"],
                    "attempt": attempt["attempt"],
                    "observation": observation,
                }
            )
            for source in ("raw", "effective"):
                group = groups[f"{phase}_{source}"]
                group["attempts"] += 1
                observed = observation.get(source, {})
                if observed.get("status") != "observed":
                    group["unavailable_attempts"] += 1
                    continue
                for fact in observed["facts"]:
                    if not fact["score_available"]:
                        group["facts_without_score"] += 1
                        continue
                    score = fact["confidence"]
                    band = next(
                        band
                        for band in group["bands"]
                        if score < band["upper"] or band["upper"] == 1
                    )
                    band["facts"] += 1
                    band["confidence_sum"] += score
                    band["facts_with_mechanical_errors"] += bool(
                        fact["mechanical_errors"]
                    )
                    band["facts_with_strict_diagnostics"] += bool(
                        fact["strict_diagnostic_rules"]
                    )
                    band["strict_diagnostics_unavailable"] += (
                        fact["strict_diagnostic_rules"] is None
                    )
    for group in groups.values():
        for band in group["bands"]:
            count = band["facts"]
            band["mean_confidence"] = (
                band.pop("confidence_sum") / count if count else None
            )
            band["mechanical_error_rate"] = (
                band["facts_with_mechanical_errors"] / count if count else None
            )
    return {
        "version": CONFIDENCE_VERSION,
        "groups": groups,
        "attempts": details,
        "semantic_calibration": "not_evaluated_without_fact_correctness_labels",
    }


def write_confidence_report(directory: Path, reports):
    summary = confidence_summary(reports)
    write_json(directory / "confidence-observation.json", summary)
    lines = [
        "# 사실별 confidence 관찰",
        "",
        "confidence는 모델 자기평가이며 보정된 정답 확률이 아닙니다.",
        "원문·표면 문자열 검사와 검증 진단을 점수에 연결합니다. 요약의 문자 차이는 별도 관찰하며 오류 수에 넣지 않습니다.",
        "유사도 보정 전후를 구분합니다. 검사 통과도 의미적 정답을 보증하지 않습니다.",
        "기존 평가의 누락·낡은 내용·관계 검사를 유지하며, 낮은 점수의 사실을 삭제하지 않습니다.",
        "의미 정답 라벨이 없어 의미적 calibration·Brier 점수는 계산하지 않습니다.",
        "동일 사실이 최초·보완에 반복될 수 있습니다. 후보의 사실 수이며 독립 표본 수가 아닙니다.",
        "",
        "엄격 진단의 0건은 전체 검사 완료를 뜻하지 않습니다. 서버 후보와 다른 원본의 진단은 미검사로 표시합니다.",
        "| 단계·후보 | 점수 구간 | 사실 수 | 평균 점수 | 문자열 검사 오류 사실 | 엄격 진단 사실 | 엄격 진단 미검사 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for name, group in summary["groups"].items():
        for band in group["bands"]:
            mean = (
                f"{band['mean_confidence']:.3f}"
                if band["mean_confidence"] is not None
                else "—"
            )
            right = "]" if band["upper"] == 1 else ")"
            lines.append(
                f"| {inline(name)} | [{band['lower']}, {band['upper']}{right} | {band['facts']} | {mean} | {band['facts_with_mechanical_errors']} | {band['facts_with_strict_diagnostics']} | {band['strict_diagnostics_unavailable']} |"
            )
    for name, group in summary["groups"].items():
        lines += [
            "",
            f"{inline(name)}: 관찰 불가 시도 {group['unavailable_attempts']}개, 점수 없는 사실 {group['facts_without_score']}개.",
            "",
        ]
    path = directory / "confidence-observation.md"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return summary
