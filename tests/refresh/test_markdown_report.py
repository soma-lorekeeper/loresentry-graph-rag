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
    assert (
        "| 시도 | 호출 범위 | 단계 | 검증 결과 | 수정 / 신규 설정 / 관계 | 실패 코드 |\n"
        "| --- | --- | --- | --- | --- | --- |\n" in text
    )
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


def test_failed_report_explains_why_omissions_and_bodies_are_unassessed(tmp_path):
    write_json(
        tmp_path / "discarded.json",
        {
            "case": "discarded",
            "result": {
                "outcome": "FAILED",
                "error": {"code": "INVALID_FACT", "retryable": False},
                "document_proposals": [],
                "new_document_proposals": [],
                "relation_proposals": [],
            },
            "assessment": {
                "rules_accepted": False,
                "assessment_availability": "unavailable_final_failure",
                "omission_metrics": "not_evaluated",
            },
        },
    )
    text = write_report(tmp_path).read_text()
    assert "추출 누락과 본문 평가는 수행하지 않았습니다" in text
    assert "수정 대상 누락 없음" not in text
    assert "필수 신규 설정 생성" not in text


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


@pytest.mark.parametrize("repaired", [False, True])
def test_report_separates_generated_failed_repaired_and_final_candidates(
    tmp_path, repaired
):
    initial = {
        "call_index": 1,
        "attempt_index": 0,
        "target_ids": ["c1"],
        "outcome": "FAILED",
        "usage": {"calls": 1, "input_tokens": 10, "output_tokens": 20},
        "document_proposals": 3,
        "new_document_proposals": 2,
        "relation_proposals": 4,
        "failure": {"code": "INVALID_EVIDENCE", "message": "private-failure-detail"},
        "diagnostics": [
            {
                "rule": "exact_source",
                "source_marker": "PRIVATE_MARKER",
                "offending_value": "private-quote",
            }
        ],
    }
    repair = {
        "call_index": 1,
        "attempt_index": 1,
        "target_ids": ["c1"],
        "outcome": "PROPOSED" if repaired else "FAILED",
        "usage": {"calls": 1, "input_tokens": 12, "output_tokens": 22},
        "document_proposals": 1,
        "new_document_proposals": 0,
        "relation_proposals": 1,
        "failure": None if repaired else {"code": "INVALID_EVIDENCE"},
        "diagnostics": [] if repaired else initial["diagnostics"],
    }
    document = {
        "target_document_id": "c1",
        "field": "body_text",
        "base_revision_no": 7,
        "value": "새 본문",
        "evidence_refs": ["PRIVATE_EVIDENCE"],
    }
    relation = {
        "document_id": "c1",
        "target_document_id": "m1",
        "relation_key": "CHARACTER",
        "reverse_relation_key": "MANUSCRIPT",
        "description": "검토할 관계 설명",
    }
    write_json(
        tmp_path / "repair.json",
        {
            "case": "repair",
            "response_source": "openai-api",
            "result": {
                "outcome": "PROPOSED" if repaired else "FAILED",
                "document_proposals": [document] if repaired else [],
                "relation_proposals": [relation] if repaired else [],
                "execution": {"attempts": [initial, repair]},
            },
        },
    )
    for index, attempt in enumerate((initial, repair), 1):
        write_json(
            tmp_path / f"repair-attempt-{index}.json",
            {
                "attempt": index,
                "usage_known": True,
                "usage": attempt["usage"],
                "raw_response": "private-raw-response",
            },
        )
    text = write_report(tmp_path).read_text()
    assert "| 호출별 후보 수(중복 포함) | 4 / 2 / 5 |" in text
    assert f"| 호출 후보 검증 실패 | {1 if repaired else 2} |" in text
    assert "| 최종 통합 검증 실패 | 0 |" in text
    assert "| 보완 시도 | 1 |" in text
    assert f"| 보완 성공 | {1 if repaired else 0} |" in text
    assert (
        f"| 최종 채택 수정 / 신규 설정 / 관계 | {'1 / 0 / 1' if repaired else '0 / 0 / 0'} |"
        in text
    )
    assert "| [1](repair-attempt-1.json) | 1 | 초기 | 검증 실패 | 3 / 2 / 4 |" in text
    assert "| [2](repair-attempt-2.json) | 1 | 보완 |" in text
    assert "| 10 | 20 |" in text and "| 12 | 22 |" in text
    for hidden in (
        "PRIVATE_MARKER",
        "private-quote",
        "PRIVATE_EVIDENCE",
        "private-failure-detail",
        "private-raw-response",
    ):
        assert hidden not in text


def test_unknown_candidate_counts_and_historical_validation_are_not_zero_or_pass(
    tmp_path,
):
    write_json(
        tmp_path / "unknown.json",
        {
            "case": "unknown",
            "result": {
                "outcome": "FAILED",
                "error": {"code": "MODEL_INVALID_RESPONSE"},
                "execution": {
                    "attempts": [
                        {
                            "call_index": 1,
                            "attempt_index": 0,
                            "outcome": "FAILED",
                            "document_proposals": None,
                            "new_document_proposals": None,
                            "relation_proposals": None,
                            "failure": {"code": "MODEL_INVALID_RESPONSE"},
                        }
                    ]
                },
            },
        },
    )
    text = write_report(tmp_path).read_text()
    assert "| 호출별 후보 수(중복 포함) | 미확인 / 미확인 / 미확인 |" in text
    assert "| 호출 후보 검증 실패 | 1 |" in text
    assert "| 최종 통합 검증 실패 | 0 |" in text
    write_json(
        tmp_path / "old.json", {"case": "old", "result": {"outcome": "NO_CHANGE"}}
    )
    write_json(
        tmp_path / "old-attempt-1.json",
        {
            "attempt": 1,
            "candidate": {
                "document_proposals": [],
                "new_document_proposals": [],
                "relation_proposals": [],
            },
        },
    )
    text = (
        write_report(tmp_path)
        .read_text()
        .split("## old", 1)[1]
        .split("## unknown", 1)[0]
    )
    assert "| 호출 후보 검증 실패 | 미확인 |" in text
    assert "| 보완 시도 | 미확인 |" in text
    assert "검증 정보 없음" in text


@pytest.mark.parametrize("code", ["INVALID_FACTS", "CONFLICTING_PROPOSALS"])
def test_historical_final_validation_error_is_not_attributed_to_the_last_candidate(
    tmp_path,
    code,
):
    write_json(
        tmp_path / "old.json",
        {
            "case": "old",
            "result": {"outcome": "FAILED", "error": {"code": code}},
        },
    )
    for index in (1, 2):
        write_json(
            tmp_path / f"old-attempt-{index}.json",
            {
                "attempt": index,
                "candidate": {
                    "document_proposals": [],
                    "new_document_proposals": [],
                    "relation_proposals": [],
                },
            },
        )
    text = write_report(tmp_path).read_text()
    assert "| [1](old-attempt-1.json) | 미확인 | 미확인 | 검증 정보 없음 |" in text
    assert "| [2](old-attempt-2.json) | 미확인 | 미확인 | 검증 정보 없음 |" in text
    assert "| 최종 통합 검증 실패 | 미확인 |" in text
    assert code.replace("_", r"\_") in text
    assert "| 보완 성공 | 미확인 |" in text


@pytest.mark.parametrize(
    "code",
    ["CONFLICTING_PROPOSALS", "INVALID_FACT", "DUPLICATE_SETTING", "REVISION_MISMATCH"],
)
@pytest.mark.parametrize("last_outcome", ["PROPOSED", "NO_CHANGE"])
def test_final_integration_error_is_separate_from_successful_call_validation(
    tmp_path, code, last_outcome
):
    attempts = [
        {
            "call_index": index,
            "attempt_index": 0,
            "outcome": "PROPOSED" if index == 1 else last_outcome,
            "document_proposals": 1,
            "new_document_proposals": 0,
            "relation_proposals": 1,
            "failure": None,
        }
        for index in (1, 2)
    ]
    write_json(
        tmp_path / "aggregate.json",
        {
            "case": "aggregate",
            "result": {
                "outcome": "FAILED",
                "error": {"code": code},
                "document_proposals": [],
                "new_document_proposals": [],
                "relation_proposals": [],
                "execution": {
                    "attempts": attempts,
                    "validation_diagnostics": [
                        {
                            "path": "facts[0]",
                            "source_marker": "HIDDEN_AGGREGATE_MARKER",
                            "offending_value": "private-aggregate-quote",
                        }
                    ],
                },
            },
        },
    )
    text = write_report(tmp_path).read_text()
    assert "| 호출 후보 검증 실패 | 0 |" in text
    assert "| 최종 통합 검증 실패 | 1 |" in text
    assert "| 호출별 후보 수(중복 포함) | 2 / 0 / 2 |" in text
    assert "| 최종 채택 수정 / 신규 설정 / 관계 | 0 / 0 / 0 |" in text
    assert "| 1 | 1 | 초기 | 검증 통과 |" in text
    assert "| 2 | 2 | 초기 | 검증 통과 |" in text
    assert (
        "HIDDEN_AGGREGATE_MARKER" not in text and "private-aggregate-quote" not in text
    )


@pytest.mark.parametrize(
    "code",
    [
        "CALL_BUDGET_EXCEEDED",
        "EVALUATION_CALL_BUDGET",
        "MODEL_AUTH_FAILED",
        "MODEL_REQUEST_INVALID",
        "MODEL_INVALID_RESPONSE",
        "STORAGE_TIMEOUT",
    ],
)
def test_model_budget_or_io_error_after_an_accepted_attempt_is_not_an_integration_validation_failure(
    tmp_path, code
):
    write_json(
        tmp_path / "non-domain.json",
        {
            "case": "non-domain",
            "result": {
                "outcome": "FAILED",
                "error": {"code": code},
                "execution": {
                    "attempts": [
                        {
                            "call_index": 1,
                            "attempt_index": 0,
                            "outcome": "PROPOSED",
                            "document_proposals": 1,
                            "new_document_proposals": 0,
                            "relation_proposals": 0,
                        }
                    ]
                },
            },
        },
    )
    text = write_report(tmp_path).read_text()
    assert "| 호출 후보 검증 실패 | 0 |" in text
    assert "| 최종 통합 검증 실패 | 0 |" in text
