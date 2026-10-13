import json
from shutil import copytree

import pytest

from app.refresh.models import Outcome
from app.refresh.rules import RefreshRules
from evaluation.novel import SETTINGS_ROOT, novel_case, novel_settings_case
from evaluation.runner import CallBudget, assemble, run_case
from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_runner import arguments


def test_setting_files_keep_pre_change_baseline_and_existing_identity():
    case = novel_settings_case("gpt-5.6-luna", "test")
    legacy = novel_case("gpt-5.6-luna", "test")
    targets = {d.document_id: d for d in case.targets}
    assert len(targets) == 8
    for document in legacy.targets:
        setting = targets[document.document_id]
        assert (setting.body_text, setting.revision_no, setting.folder_code) == (
            document.body_text,
            document.revision_no,
            document.folder_code,
        )
    assert "황동 덮개" in targets["character-doyun"].body_text
    assert "황동 덮개" in targets["item-finger"].body_text
    assert all(
        "강연오" not in d.body_text and "검게 산화시킨 강철" not in d.body_text
        for d in case.targets
    )
    assert all(dict(d.properties).get("name") for d in case.targets)
    assert all(d.title == dict(d.properties)["name"] for d in case.targets)
    assert dict(targets["character-doyun"].properties)["alias"] == "도윤"
    assert "완전한 폐지" in targets["worldview-memory-tax"].body_text
    assert "한 달 동안 쉬어야" in targets["character-yunjun"].body_text
    assert {d.document_id for d in case.source.documents} == {
        "chapter-05",
        "chapter-06",
    }
    assert case.request.settings.separate_discovery
    assert not legacy.request.settings.separate_discovery


def test_expanded_service_includes_all_settings_and_creates_new_actor_once():
    case = novel_settings_case("gpt-5.6-luna", "test")
    before = repr((case.source, case.targets, case.related))
    budget = CallBudget(9)
    scenario = assemble(case, None, budget)
    result, assessment = run_case(case, scenario, repeat_saved=True)
    assert result.outcome == Outcome.PROPOSED
    assert len(result.document_proposals) == 3
    assert len(result.new_document_proposals) == 1
    assert result.new_document_proposals[0].candidate_id == "new:CHARACTER:강연오"
    assert len(result.relation_proposals) == 2
    assert assessment["rules_accepted"]
    assert assessment["manuscript_content_preserved"]
    assert assessment["required_content_preserved"]
    assert assessment["expected_new_settings"]
    assert assessment["expected_relations"]
    assert budget.used == 9
    assert repr((case.source, case.targets, case.related)) == before
    snapshot = scenario.artifacts.read_context(case.request)
    assert len(snapshot.documents) == 10
    assert set(snapshot.target_ids) == {d.document_id for d in case.targets}
    rules = RefreshRules()
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert len(inputs) == 9
    assert inputs[0].target_ids == ()
    assert [item.allow_new_documents for item in inputs] == [True] + [False] * 8
    assert [item.allow_document_updates for item in inputs] == [False] + [True] * 8
    assert [item.target_ids for item in inputs[1:]] == [
        (target,) for target in snapshot.target_ids
    ]
    for item in inputs:
        context = json.loads(item.prompt)
        assert {d["document_id"] for d in context["documents"]} == {
            d.document_id for d in snapshot.documents
        }
        assert {e["document_id"] for e in context["evidence"]} == {
            d.document_id for d in snapshot.documents
        }
    sources = {d.document_id: d for d in case.source.documents}
    for proposal in result.new_document_proposals:
        for evidence in proposal.evidence:
            assert (
                sources[evidence.document_id].body_text[evidence.start : evidence.end]
                == evidence.quote
            )


def test_markdown_edit_changes_loaded_context_and_fingerprint(tmp_path):
    directory = tmp_path / "settings"
    copytree(SETTINGS_ROOT, directory)
    original = novel_settings_case("gpt-5.6-luna", "test", directory)
    path = directory / "character-yunjun.md"
    path.write_text(path.read_text() + "\n평가용 추가 문장.\n")
    changed = novel_settings_case("gpt-5.6-luna", "test", directory)
    assert (
        original.request.job.input_fingerprint != changed.request.job.input_fingerprint
    )
    assert "평가용 추가 문장." in next(
        d.body_text for d in changed.targets if d.document_id == "character-yunjun"
    )


def test_expanded_cli_writes_reviewable_report_without_network(tmp_path):
    output = tmp_path / "expanded"
    assert (
        main(
            arguments(output)
            + ["--suite", "novel-settings", "--max-calls", "9", "--repeat-saved"]
        )
        == 2
    )
    report = json.loads((output / "novel-settings.json").read_text())
    assert not report["live"]
    assert report["assessment"]["body_claim_mismatches"] == []
    assert report["assessment"]["unreviewed_body_claims"] == [
        "서명과 현재 동의를 구분하는 본문 표현: 본문 표현 미확인"
    ]
    assert report["result"]["execution"]["usage"]["calls"] == 9
    assert len(report["snapshot"]["documents"]) == 10
    assert len(list(output.glob("*-attempt-*.json"))) == 9
    first = json.loads((output / "novel-settings-attempt-1.json").read_text())
    assert not first["model_input"]["allow_document_updates"]
    assert first["model_input"]["target_ids"] == []
    assert not first["candidate"]["document_proposals"]
    text = (output / "report.md").read_text()
    assert "강연오 · CHARACTER" in text
    assert "기억세와 기억 반환" in text
    assert "고정 기대값" in text


def test_expanded_cli_rejects_explicit_eight_call_budget_before_output(tmp_path):
    output = tmp_path / "too-small"
    with pytest.raises(SystemExit) as caught:
        main(arguments(output) + ["--suite", "novel-settings"])
    assert caught.value.code == 2
    assert not output.exists()


def test_expanded_cli_uses_nine_call_default_when_no_budget_is_configured(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENAI_MAX_CALLS", raising=False)
    output = tmp_path / "default-budget"
    values = arguments(output)
    offset = values.index("--max-calls")
    del values[offset : offset + 2]
    assert main(values + ["--suite", "novel-settings"]) == 2
    report = json.loads((output / "novel-settings.json").read_text())
    assert report["result"]["execution"]["usage"]["calls"] == 9
    assert report["assessment"]["body_claim_mismatches"] == []
    assert report["assessment"]["unreviewed_body_claims"] == [
        "서명과 현재 동의를 구분하는 본문 표현: 본문 표현 미확인"
    ]


def test_expanded_cli_preserves_environment_call_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_MAX_CALLS", "8")
    output = tmp_path / "environment-budget"
    values = arguments(output)
    offset = values.index("--max-calls")
    del values[offset : offset + 2]
    with pytest.raises(SystemExit):
        main(values + ["--suite", "novel-settings"])
    assert not output.exists()


@pytest.mark.parametrize("filename", ["../context.json", "/tmp/file.md"])
def test_manifest_cannot_read_outside_setting_directory(tmp_path, filename):
    copytree(SETTINGS_ROOT, tmp_path / "settings")
    manifest = tmp_path / "settings/manifest.json"
    data = json.loads(manifest.read_text())
    data["documents"][0]["file"] = filename
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="local Markdown filename"):
        novel_settings_case("gpt-5.6-luna", "test", tmp_path / "settings")
