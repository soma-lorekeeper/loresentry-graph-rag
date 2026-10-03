import json
from pathlib import Path
from shutil import copytree

import pytest

from app.refresh.models import Outcome
from evaluation.novel import FIXTURE_ROOT, novel_case
from evaluation.runner import CallBudget, assemble, run_case
from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_runner import arguments

NOVEL = Path(__file__).resolve().parents[2] / "docs/examples/novel/the-thirteenth-bell"


def test_fake_s3_contains_only_changed_chapters_and_preserves_exact_body():
    case = novel_case("gpt-5.6-luna", "test")
    assert [(d.document_id, d.revision_no) for d in case.source.documents] == [
        ("chapter-05", 2),
        ("chapter-06", 1),
    ]
    assert [(d.document_id, d.revision_no) for d in case.request.changed_documents] == [
        ("chapter-05", 2),
        ("chapter-06", 1),
    ]
    for doc, name in zip(
        case.source.documents,
        ("05-the-new-map.md", "06-the-empty-address.md"),
        strict=True,
    ):
        assert doc.body_text == (NOVEL / name).read_text().split("\n\n", 1)[1].rstrip(
            "\n"
        )
        assert 4800 <= len(doc.body_text) <= 5000
    assert "황동" in (NOVEL / "baseline/05-the-new-map.rev1.md").read_text()
    assert "검게 산화시킨 강철" in case.source.documents[0].body_text
    assert "황동 덮개" in case.targets[0].body_text


def test_novel_service_uses_content_fake_and_leaves_originals_unchanged():
    case = novel_case("gpt-5.6-luna", "test")
    before = repr((case.source, case.targets, case.related))
    budget = CallBudget(3)
    scenario = assemble(case, None, budget)
    result, assessment = run_case(case, scenario, repeat_saved=True)
    assert result.outcome == Outcome.PROPOSED
    assert len(result.document_proposals) == 3
    assert len(result.relation_proposals) == 1
    assert assessment["rules_accepted"]
    assert budget.used == 3
    assert scenario.calls.names().count("documents.fetch_documents") == 1
    assert repr((case.source, case.targets, case.related)) == before
    payload = json.loads(next(iter(scenario.artifacts.json_results.values())))
    assert {d["document_id"] for d in payload["sources"] if d["origin"] == "INPUT"} == {
        "chapter-05",
        "chapter-06",
    }
    assert {
        d["document_id"] for d in payload["sources"] if d["origin"] == "CONTENT"
    } == {"character-doyun", "character-haeju", "item-finger"}


def test_novel_replay_runs_without_api(tmp_path, monkeypatch):
    import scripts.evaluate_refresh as cli

    def forbidden(**kwargs):
        raise AssertionError("Remote model must not be created")

    monkeypatch.setattr(cli, "OpenAI", forbidden)
    output = tmp_path / "novel"
    assert (
        main(
            arguments(output)
            + [
                "--suite",
                "novel",
                "--responses-dir",
                str(FIXTURE_ROOT / "responses"),
                "--repeat-saved",
            ]
        )
        == 0
    )
    report = json.loads((output / "novel-refresh.json").read_text())
    assert report["result"]["execution"]["usage"]["calls"] == 3
    assert report["assessment"]["required_content_preserved"]
    assert report["assessment"]["expected_relations"]
    assert not report["live"]


def test_json_body_change_changes_identity_and_enforces_input_limit(tmp_path):
    directory = tmp_path / "fixture"
    copytree(FIXTURE_ROOT, directory)
    original = novel_case("gpt-5.6-luna", "test", directory)
    path = directory / "input.json"
    data = json.loads(path.read_text())
    data["documents"][0]["body_text"] = "가" * 5001
    path.write_text(json.dumps(data))
    changed = novel_case("gpt-5.6-luna", "test", directory)
    assert (
        changed.request.job.input_fingerprint != original.request.job.input_fingerprint
    )
    budget = CallBudget(3)
    result, _ = run_case(changed, assemble(changed, None, budget))
    assert result.failure.code == "BODY_TOO_LARGE"
    assert budget.used == 0


def test_json_revision_is_not_coerced(tmp_path):
    from pydantic import ValidationError

    directory = tmp_path / "fixture"
    copytree(FIXTURE_ROOT, directory)
    path = directory / "input.json"
    data = json.loads(path.read_text())
    data["documents"][0]["revision_no"] = "2"
    path.write_text(json.dumps(data))
    with pytest.raises(ValidationError):
        novel_case("gpt-5.6-luna", "test", directory)
