"""저장된 평가 JSON을 사람이 검토할 Markdown으로 변환한다. 원격 호출은 없다."""

import json
import os
import re
from difflib import unified_diff
from html import escape
from pathlib import Path
from urllib.parse import quote

from evaluation.cases import (
    CHECKS,
    LIST_CHECKS,
    REVIEW_CHECKS,
    evaluation_outcome,
    failed_checks,
)

FOLDER_LABELS = {
    "MANUSCRIPT": "원고",
    "CHARACTER": "캐릭터",
    "WORLDVIEW": "세계관",
    "LOCATION": "장소",
    "ORGANIZATION": "조직",
    "ITEM": "아이템",
    "EVENT": "이벤트",
}


def folder_label(code):
    label = FOLDER_LABELS.get(code)
    return inline(f"{code} ({label})" if label else code)


def inline(value):
    """외부 텍스트가 Markdown 구조나 HTML로 해석되지 않게 한다."""
    text = escape(str(value), quote=False)
    text = re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", text)
    return text.replace("\r", " ").replace("\n", " ")


def block(text, language="text"):
    text = str(text)
    fence = "`" * max(3, max((len(m) + 1 for m in re.findall(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}"


def status(report):
    return {
        "execution_failed": "실행 실패",
        "unassessed": "검사 정보 없음",
        "failed": "검사 불일치",
        "review_required": "검토 필요",
        "passed": "자동 검사 통과",
    }[evaluation_outcome(report)]


PROPOSAL_FIELDS = (
    "document_proposals",
    "new_document_proposals",
    "relation_proposals",
)


def proposal_counts(candidate):
    return {
        key: len(candidate[key]) if isinstance(candidate.get(key), list) else None
        for key in PROPOSAL_FIELDS
    }


def attempt_details(report, attempts):
    """실행별 검증 결과를 원본 기록과 연결한다. 과거의 없는 검증 상태는 추측하지 않는다."""
    result = report.get("result", {})
    tracked = result.get("execution", {}).get("attempts", [])
    details = []
    for index in range(max(len(tracked), len(attempts))):
        filename, record = attempts[index] if index < len(attempts) else (None, {})
        validation = (
            tracked[index] if index < len(tracked) else record.get("validation", {})
        )
        candidate = record.get("candidate") or {}
        counts = proposal_counts(candidate)
        counts.update(
            {key: validation[key] for key in PROPOSAL_FIELDS if key in validation}
        )
        failure = validation.get("failure") or record.get("failure")
        outcome = validation.get("outcome")
        if outcome in {"PROPOSED", "NO_CHANGE"}:
            state = "validated"
        elif failure:
            state = (
                "validation_failed"
                if candidate
                or validation.get("diagnostics")
                or failure.get("code") == "MODEL_INVALID_RESPONSE"
                else "model_failed"
            )
        else:
            state = "unknown"
        details.append(
            {
                "filename": filename,
                "record": record,
                "call_index": validation.get("call_index", record.get("call_index")),
                "attempt_index": validation.get(
                    "attempt_index", record.get("attempt_index")
                ),
                "state": state,
                "counts": counts,
                "failure": failure or {},
            }
        )
    return details


def known_total(values):
    known = [value for value in values if type(value) is int]
    total = sum(known)
    if len(known) == len(values):
        return str(total)
    return f"{total} + 미확인" if total else "미확인"


def integration_validation_failure(report):
    """검증을 통과한 호출들 이후의 통합 오류만 집계한다. 과거 단계는 추정하지 않는다."""
    result = report.get("result", {})
    if result.get("outcome") in {"PROPOSED", "NO_CHANGE"}:
        return "0"
    failure = report.get("failure") or result.get("error") or {}
    code = failure.get("code", "")
    domain_error = code.startswith(
        ("INVALID_", "CONFLICTING_", "UNKNOWN_")
    ) or code in {
        "REVISION_MISMATCH",
        "DUPLICATE_SETTING",
    }
    if code and not domain_error:
        # Model, budget and IO failures do not come from candidate integration.
        return "0"
    tracked = result.get("execution", {}).get("attempts", [])
    if tracked and result.get("outcome") == "FAILED":
        last_outcome = tracked[-1].get("outcome")
        if last_outcome == "FAILED":
            return "0"
        if domain_error and last_outcome in {"PROPOSED", "NO_CHANGE"}:
            return "1"
    return "미확인"


def generation_lines(report, attempts):
    result = report.get("result", {})
    details = attempt_details(report, attempts)
    final = " / ".join(str(len(result.get(key, []))) for key in PROPOSAL_FIELDS)
    generated = (
        " / ".join(
            known_total([detail["counts"][key] for detail in details])
            for key in PROPOSAL_FIELDS
        )
        if details
        else "미확인"
    )
    failures = (
        known_total(
            [
                None
                if detail["state"] == "unknown"
                else int(detail["state"] == "validation_failed")
                for detail in details
            ]
        )
        if details
        else "기록 없음"
    )
    repairs = (
        known_total(
            [
                int(detail["attempt_index"] > 0)
                if type(detail["attempt_index"]) is int
                else None
                for detail in details
            ]
        )
        if details
        else "기록 없음"
    )
    successes = (
        known_total(
            [
                None
                if detail["attempt_index"] is None
                or (detail["attempt_index"] > 0 and detail["state"] == "unknown")
                else int(detail["attempt_index"] > 0 and detail["state"] == "validated")
                for detail in details
            ]
        )
        if details
        else "기록 없음"
    )
    lines = [
        "### 생성·검증·보완 현황",
        "후보 수는 수정 / 신규 설정 / 관계 순서입니다. 호출별 합계는 재생성된 후보의 중복을 포함하며, 최종 채택 수는 최종 결과의 제안만 집계합니다.",
        "| 항목 | 수 |",
        "| --- | --- |",
        f"| 기록된 모델 시도 | {len(details) if details else '기록 없음'} |",
        f"| 호출별 후보 수(중복 포함) | {generated} |",
        f"| 호출 후보 검증 실패 | {failures} |",
        f"| 최종 통합 검증 실패 | {integration_validation_failure(report)} |",
        f"| 보완 시도 | {repairs} |",
        f"| 보완 성공 | {successes} |",
        f"| 최종 채택 수정 / 신규 설정 / 관계 | {final} |",
        "",
        "| 시도 | 호출 범위 | 단계 | 검증 결과 | 수정 / 신규 설정 / 관계 | 실패 코드 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    states = {
        "validated": "검증 통과",
        "validation_failed": "검증 실패",
        "model_failed": "모델 호출 실패",
        "unknown": "검증 정보 없음",
    }
    for index, detail in enumerate(details, 1):
        link = (
            f"[{index}]({quote(detail['filename'])})"
            if detail["filename"]
            else str(index)
        )
        phase = (
            "미확인"
            if detail["attempt_index"] is None
            else "보완"
            if detail["attempt_index"]
            else "초기"
        )
        counts = " / ".join(
            str(detail["counts"][key])
            if detail["counts"][key] is not None
            else "미확인"
            for key in PROPOSAL_FIELDS
        )
        state = states[detail["state"]]
        lines.append(
            f"| {link} | {detail['call_index'] or '미확인'} | {phase} | {state} | {counts} | {inline(detail['failure'].get('code', '없음'))} |"
        )
    if not details:
        lines.append("\n시도별 생성·검증·보완 기록이 없습니다.")
    return lines


def case_lines(filename, report, attempts):
    result = report.get("result", {})
    lines = [
        f"## {inline(report['case'])}",
        f"판정: **{status(report)}** · 결과: {inline(result.get('outcome', '미생성'))}",
        f"원본: [{inline(filename)}]({quote(filename)})",
        f"요청: {inline(result.get('request_id', report.get('trial', '미확인')))} · "
        f"모델 설정: {inline(report.get('settings', {}).get('model', '미확인'))}",
    ]
    failure = report.get("failure") or result.get("error")
    if failure:
        lines.append(
            f"실패 코드: {inline(failure.get('code', '미확인'))} · "
            f"재시도 가능: {inline(failure.get('retryable', '미확인'))}"
        )
    if report.get("unexpected_error_type"):
        lines.append(f"예외 종류: {inline(report['unexpected_error_type'])}")
    if report.get("assessment"):
        lines.extend(["### 자동 검사", "| 검사 | 결과 |", "| --- | --- |"])
        bad = failed_checks(report)
        for key, label in CHECKS.items():
            if key not in report["assessment"]:
                continue
            value = "불일치" if key in bad else "통과"
            if key in LIST_CHECKS and key in bad:
                value += ": " + inline(", ".join(report["assessment"][key]))
            lines.append(f"| {label} | {value} |")
        for key, label in REVIEW_CHECKS.items():
            pending = report["assessment"].get(key, [])
            if pending:
                lines.append(f"| {label} | 검토 필요: {inline(', '.join(pending))} |")
        semantic = report["assessment"].get("semantic_assessment", {})
        if semantic.get("criteria_count"):
            lines.append(
                "\n역할·수령인·범위 검사는 등록된 사실 기준을 비교합니다. "
                "전체 본문의 의미와 사실 해석은 별도 검토가 필요합니다."
            )
        if report["assessment"].get("body_assessment", {}).get("criteria_count"):
            lines.append(
                "본문 검사는 등록된 표현의 누락과 잘못된 단정을 확인합니다. "
                "표현이 일치해도 전체 의미와 사실 연결은 검토 대기로 남습니다."
            )
        if (
            report["assessment"].get("assessment_availability")
            == "unavailable_final_failure"
        ):
            lines.append(
                "최종 실패로 제안 전체가 폐기되어 추출 누락과 본문 평가는 수행하지 않았습니다."
            )
    lines.extend(generation_lines(report, attempts))
    lines.extend(
        [
            "### 응답 시간과 사용량",
            "시간은 모델 어댑터 시도 단위이며 전체 refresh 소요 시간이 아닙니다. "
            "미확인 사용량을 0으로 계산하지 않습니다.",
            "| 시도 | 실제 모델 | 시간(초) | 입력 토큰 | 출력 토큰 | 실패 코드 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for attempt_file, attempt in attempts:
        usage = attempt.get("usage") if attempt.get("usage_known") else None
        elapsed = attempt.get("elapsed_seconds")
        duration = f"{elapsed:.3f}" if elapsed is not None else "미확인"
        error = attempt.get("failure") or {}
        lines.append(
            f"| [{attempt.get('attempt', '?')}]({quote(attempt_file)}) "
            f"| {inline(attempt.get('response_model', '미확인'))} | {duration} "
            f"| {(usage or {}).get('input_tokens', '미확인')} "
            f"| {(usage or {}).get('output_tokens', '미확인')} "
            f"| {inline(error.get('code', '없음'))} |"
        )
    if not attempts:
        lines.append("\n시도 기록이 없습니다.")
    documents = {
        d["document_id"]: d for d in (report.get("snapshot") or {}).get("documents", [])
    }
    if documents:
        lines.append("### 입력과 기존 설정")
        origins = {
            source["document_id"]: source["origin"]
            for source in result.get("sources", [])
        }
        context = []
        for target, document in documents.items():
            origin = {"INPUT": "변경 입력", "CONTENT": "기존 설정"}.get(
                origins.get(target), "스냅샷"
            )
            context.extend(
                [
                    f"#### {inline(target)} · {origin} · "
                    f"{folder_label(document.get('folder_code', '미확인'))} · "
                    f"revision {document['revision_no']}",
                    block(document.get("body_text") or ""),
                ]
            )
            if document.get("properties"):
                context.append(
                    block(
                        json.dumps(
                            dict(document["properties"]), ensure_ascii=False, indent=2
                        ),
                        "json",
                    )
                )
        lines.append(
            "<details>\n<summary>검토에 사용된 문서 펼치기</summary>\n\n"
            + "\n\n".join(context)
            + "\n\n</details>"
        )
    lines.append("### 기존 문서 수정 제안")
    for proposal in result.get("document_proposals", []):
        target, field = proposal["target_document_id"], proposal["field"]
        document = documents.get(target)
        before = None
        if document is not None:
            before = (
                document.get("body_text")
                if field == "body_text"
                else dict(document.get("properties", [])).get(field)
            )
        lines.append(
            f"#### {inline(target)} · {inline(field)} · "
            f"기준 revision {proposal['base_revision_no']}"
        )
        if before is None:
            lines.append("이전 값은 스냅샷에서 확인할 수 없습니다.")
            lines.append(block(proposal["value"]))
        else:
            diff = "\n".join(
                unified_diff(
                    before.splitlines(),
                    proposal["value"].splitlines(),
                    fromfile="이전",
                    tofile="제안",
                    lineterm="",
                )
            )
            lines.append(block(diff, "diff") if diff else "값의 변화 없음")
    if not result.get("document_proposals"):
        lines.append("없음")
    lines.append("### 신규 설정 생성 제안")
    for proposal in result.get("new_document_proposals", []):
        lines.extend(
            [
                f"#### {inline(proposal['name'])} · {folder_label(proposal['folder_code'])}",
                f"임시 ID: {inline(proposal['candidate_id'])}",
                block(proposal["body_text"]),
            ]
        )
    if not result.get("new_document_proposals"):
        lines.append("없음")
    lines.append("### 관계 제안")
    for proposal in result.get("relation_proposals", []):
        lines.extend(
            [
                f"#### {inline(proposal.get('operation', 'ADD'))}: "
                f"{inline(proposal['document_id'])} → "
                f"{inline(proposal['target_document_id'])}",
                f"관계 키: {inline(proposal['relation_key'])} · "
                f"역방향 키: {inline(proposal['reverse_relation_key'])}",
                block(proposal["description"]),
            ]
        )
    if not result.get("relation_proposals"):
        lines.append("없음")
    return lines


def write_report(directory: Path) -> Path:
    """평가 디렉터리를 읽어 report.md를 생성한다. JSON 원본은 변경하지 않는다."""
    cases = []
    for path in sorted(directory.glob("*.json")):
        if "-attempt-" in path.stem:
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or "case" not in report:
            continue
        attempts = [
            (p.name, json.loads(p.read_text(encoding="utf-8")))
            for p in directory.glob(f"{path.stem}-attempt-*.json")
        ]
        attempts.sort(key=lambda item: item[1].get("attempt", 0))
        cases.append((path.name, report, attempts))
    if not cases:
        raise ValueError("No evaluation case JSON files found")
    lines = [
        "# Refresh 평가 보고서",
        "저장된 JSON을 정리한 보고서입니다. 추가 모델 호출이나 의미상 품질 판정은 "
        "수행하지 않습니다. 자동 검사 통과 후에도 원고에 없는 사실의 발명, 누락, "
        "기존 설정 보존과 관계의 타당성은 사람이 검토해야 합니다.",
        "S3·Content·관계 조회·상태 저장·Kafka는 평가용 fake입니다. "
        "제안이 실제 문서에 반영됐거나 운영 연동이 검증됐다는 뜻은 아닙니다.",
        "| 사례 | 응답 출처 | 판정 | 결과 | 최종 채택 수정 / 신규 설정 / 관계 |",
        "| --- | --- | --- | --- | --- |",
    ]
    sources = {
        "openai-api": "실제 API",
        "conversation-assistant": "고정 응답 재생",
        "expected-fixture": "고정 기대값",
    }
    for filename, report, _ in cases:
        result = report.get("result", {})
        counts = " / ".join(
            str(len(result.get(key, [])))
            for key in (
                "document_proposals",
                "new_document_proposals",
                "relation_proposals",
            )
        )
        source = sources.get(report.get("response_source"), "미확인")
        lines.append(
            f"| [{inline(report['case'])}]({quote(filename)}) | {source} "
            f"| {status(report)} | {inline(result.get('outcome', '미생성'))} | {counts} |"
        )
    for args in cases:
        lines.extend(case_lines(*args))
    # Keep report permissions aligned with the potentially private source JSON.
    target = directory / "report.md"
    temporary = directory / "report.md.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for index, line in enumerate(lines):
                separator = (
                    "\n"
                    if index
                    and line.startswith("|")
                    and lines[index - 1].startswith("|")
                    else "\n\n"
                )
                stream.write((separator if index else "") + line)
            stream.write("\n")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
