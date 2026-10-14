"""보완 전후의 삭제·재작성 관찰. 문자열 차이를 의미 손실로 확정하지 않는다."""

import json
import re
from dataclasses import asdict
from pathlib import Path

from evaluation.artifacts import write_json
from evaluation.markdown import inline

LISTS = ("document_proposals", "new_document_proposals", "relation_proposals")


def key(name, proposal):
    if name == "document_proposals":
        return (proposal["target_document_id"], proposal["field"])
    if name == "new_document_proposals":
        return (proposal["candidate_id"],)
    return tuple(sorted((proposal["document_id"], proposal["target_document_id"])))


def compare(previous, candidate):
    changes = []
    for name in LISTS:
        current = {key(name, p): p for p in candidate.get(name, [])}
        for old in previous.get(name, []):
            identity = key(name, old)
            new = current.get(identity)
            field = "value" if name == "document_proposals" else "body_text"
            old_body = old.get(field, "")
            new_body = new.get(field, "") if new else ""
            if new is None or old_body != new_body:
                fragments = [
                    part.strip()
                    for part in re.split(r"(?<=[.!?。])\s+|\n+", old_body)
                    if part.strip() and part.strip() not in new_body
                ]
                changes.append(
                    {
                        "list": name,
                        "key": identity,
                        "removed": new is None,
                        "body_changed": old_body != new_body,
                        "previous_fragments_absent": fragments,
                    }
                )
    statements = {fact["statement"] for fact in candidate.get("facts", [])}
    return {
        "proposal_changes": changes,
        "previous_fact_statements_absent": [
            fact["statement"]
            for fact in previous.get("facts", [])
            if fact["statement"] not in statements
        ],
        "semantic_verdict": "human_review_pending",
    }


def observe_repair(model_input, raw, effective):
    repair = json.loads(model_input.prompt).get("validation_repair", {})
    previous = repair.get("previous_candidate")
    if not isinstance(previous, dict) or not all(
        isinstance(previous.get(name), list) for name in LISTS
    ):
        return {"status": "unavailable", "reason": "previous_candidate_unparsed"}
    raw_value, effective_value = asdict(raw), asdict(effective)
    return {
        "status": "observed",
        "preservation_applied": raw_value != effective_value,
        "raw_changes": compare(previous, raw_value),
        "effective_changes": compare(previous, effective_value),
    }


def write_repair_report(directory: Path, reports):
    observations = [
        {
            "case": report["case"],
            "attempt": attempt["attempt"],
            **attempt["repair_observation"],
        }
        for report in reports
        for attempt in report.get("observed_attempts", [])
        if "repair_observation" in attempt
    ]
    if not observations:
        return
    write_json(directory / "repair-observation.json", {"attempts": observations})
    lines = [
        "# 보완 전후 내용 관찰",
        "",
        "모델 원본과 서버 보존 후 후보를 각각 이전 후보와 비교합니다.",
        "문장 부재는 재서술·정당한 오류 제거도 포함하므로 의미 손실 여부는 원문 검토가 필요합니다.",
    ]
    for item in observations:
        lines += ["", f"## {inline(item['case'])} / 시도 {item['attempt']}", ""]
        if item["status"] != "observed":
            lines.append("이전 후보를 파싱할 수 없어 비교할 수 없습니다.")
            continue
        lines += [f"서버 보존 적용: {item['preservation_applied']}", ""]
        for field, label in (
            ("raw_changes", "모델 원본"),
            ("effective_changes", "서버 보존 후"),
        ):
            changes = item[field]["proposal_changes"]
            lines += [f"### {label}", "", f"삭제·본문 변경 제안: {len(changes)}개", ""]
            for change in changes:
                lines.append(
                    f"- {inline(change['key'])}; 제안 삭제: {change['removed']}"
                )
                lines.extend(
                    f"  - 이전 문장 부재: {inline(part)}"
                    for part in change["previous_fragments_absent"]
                )
    path = directory / "repair-observation.md"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
