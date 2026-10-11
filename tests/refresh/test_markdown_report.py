import json

import pytest

from evaluation.artifacts import write_json
from evaluation.markdown import block, write_report
from scripts.evaluate_refresh import main
from scripts.report_refresh import main as report_main
from tests.refresh.test_evaluation_runner import arguments


def test_report_shows_changes_without_evidence_and_preserves_json(tmp_path):
    target = tmp_path / "run"
    assert main(arguments(target) + ["--case", "both"]) == 0
    text = (target / "report.md").read_text()
    assert "-유나는 붉은 망토" in text
    assert "+유나는 파란 망토" in text
    assert "고향은 서울이다." in text
    assert "ADD: c1 → i1" in text
    assert "제안 값:" not in text
    assert "```text\n유나는 파란 망토를 입는다. 고향은 서울이다.\n```" not in text
    assert "근거 E" not in text
    assert "E0001" not in text
    assert "[0:31]" not in text
    result = json.loads((target / "both.json").read_text())["result"]
    assert result["evidence"]
    assert all(p["evidence_refs"] for p in result["document_proposals"])
    assert all(p["evidence_refs"] for p in result["relation_proposals"])
    assert "고정 기대값" in text
    assert "미확인 | 미확인 | 없음" in text
    assert "| --- | --- |\n| 제안 검증 규칙 통과 | 통과 |" in text
    assert (target / "report.md").stat().st_mode & 0o777 == 0o600
    originals = {p.name: p.read_bytes() for p in target.glob("*.json")}
    (target / "report.md").unlink()
    assert report_main([str(target)]) == 0
    assert (target / "report.md").read_text() == text
    assert {p.name: p.read_bytes() for p in target.glob("*.json")} == originals


def test_missing_previous_value_still_shows_proposed_content(tmp_path):
    target = tmp_path / "run"
    assert main(arguments(target) + ["--case", "document"]) == 0
    path = target / "document.json"
    report = json.loads(path.read_text())
    report["snapshot"] = None
    write_json(path, report)
    text = write_report(target).read_text()
    assert "이전 값은 스냅샷에서 확인할 수 없습니다." in text
    assert "```text\n유나는 파란 망토를 입는다. 고향은 서울이다.\n```" in text


def test_new_settings_report_shows_creation_and_temporary_relation(tmp_path):
    target = tmp_path / "new"
    assert main(arguments(target) + ["--suite", "new-settings"]) == 0
    text = (target / "report.md").read_text()
    assert "민수 · CHARACTER" in text
    assert "임시 ID: new:CHARACTER:민수" in text
    assert "민수는 항구의 경비병이다." in text
    assert "new:CHARACTER:민수" in text and "→" in text
    assert "근거 E" not in text and "E0001" not in text
    result = json.loads((target / "new-character-only.json").read_text())["result"]
    assert result["new_document_proposals"][0]["evidence_refs"]


def test_no_change_keeps_source_and_existing_setting_available_for_review(tmp_path):
    target = tmp_path / "unchanged"
    assert main(arguments(target) + ["--case", "no-change"]) == 0
    text = (target / "report.md").read_text()
    assert "검토에 사용된 문서 펼치기" in text
    assert "m1 · 변경 입력" in text
    assert "c1 · 기존 설정" in text
    assert "유나는 파란 망토를 입는다. 고향은 서울이다." in text


def test_successful_execution_still_reports_assessment_mismatch(tmp_path):
    target = tmp_path / "mismatch"
    assert main(arguments(target) + ["--case", "document"]) == 0
    path = target / "document.json"
    report = json.loads(path.read_text())
    report["assessment"]["missing_targets"] = ["missing"]
    report["assessment"]["required_content_preserved"] = False
    write_json(path, report)
    write_report(target)
    text = (target / "report.md").read_text()
    assert "검사 불일치" in text
    assert "수정 대상 누락 없음 | 불일치: missing" in text
    assert "필수 내용 보존 | 불일치" in text


def test_failure_report_retains_known_usage_without_provider_raw_error(tmp_path):
    write_json(
        tmp_path / "document.json",
        {
            "case": "document",
            "live": True,
            "response_source": "openai-api",
            "failure": {"code": "MODEL_AUTH_FAILED", "retryable": False},
            "snapshot": None,
        },
    )
    write_json(
        tmp_path / "document-attempt-1.json",
        {
            "attempt": 1,
            "elapsed_seconds": 1.2345,
            "usage_known": True,
            "usage": {"input_tokens": 12, "output_tokens": 34},
            "failure": {"code": "MODEL_AUTH_FAILED"},
            "raw_error": "private-provider-data",
            "raw_response": "private-response",
        },
    )
    text = write_report(tmp_path).read_text()
    assert "실행 실패" in text and r"MODEL\_AUTH\_FAILED" in text
    assert "1.234 | 12 | 34" in text
    assert "private-provider-data" not in text
    assert "private-response" not in text


def test_quoted_markdown_cannot_close_content_fence():
    text = "before\n```\n# heading\n````\nafter"
    rendered = block(text)
    assert rendered.startswith("`````text\n")
    assert rendered.endswith("\n`````")
    assert text in rendered


def test_missing_evaluation_directory_does_not_create_empty_report(tmp_path):
    with pytest.raises(SystemExit):
        report_main([str(tmp_path / "missing")])
    assert not (tmp_path / "missing").exists()
