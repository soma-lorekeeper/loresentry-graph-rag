import json

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure
from evaluation.cases import cases
from evaluation.runner import CallBudget, assemble, run_case
from scripts.evaluate_refresh import main


def arguments(path):
    return [
        "--model",
        "gpt-5.6-luna",
        "--timeout",
        "10",
        "--max-input-tokens",
        "120000",
        "--max-output-tokens",
        "8192",
        "--context-window",
        "128192",
        "--max-calls",
        "8",
        "--trial",
        "test",
        "--output",
        str(path),
    ]


def test_offline_cli_all_cases_without_network(tmp_path):
    target = tmp_path / "run"
    assert main(arguments(target) + ["--repeat-saved"]) == 0
    assert len(list(target.glob("*.json"))) == 13
    report = json.loads((target / "both.json").read_text())
    assert not report["live"]
    assert report["assessment"]["rules_accepted"]
    assert report["assessment"]["manuscript_content_preserved"]
    assert report["result"]["execution"]["usage"]["calls"] == 2
    with pytest.raises(SystemExit):
        main(arguments(target))


@pytest.mark.parametrize("change", [" ", "\n", "다른 문장"])
def test_assessment_detects_manuscript_changes(change):
    from dataclasses import replace

    case = cases("gpt-5.6-luna", "test")[0]
    scenario = assemble(case, None, CallBudget(2))
    _, assessment = run_case(case, scenario)
    assert assessment["manuscript_content_preserved"]
    for key, snapshot in scenario.artifacts.contexts.items():
        scenario.artifacts.contexts[key] = replace(
            snapshot,
            documents=tuple(
                replace(d, body_text=d.body_text + change)
                if d.folder_code == "MANUSCRIPT"
                else d
                for d in snapshot.documents
            ),
        )
    _, assessment = run_case(case, scenario, repeat_saved=True)
    assert not assessment["manuscript_content_preserved"]


def test_cli_fails_when_manuscript_preservation_check_fails(tmp_path, monkeypatch):
    import scripts.evaluate_refresh as cli

    original_run = cli.run_case

    def changed_manuscript(*args, **kwargs):
        result, assessment = original_run(*args, **kwargs)
        assessment["manuscript_content_preserved"] = False
        return result, assessment

    monkeypatch.setattr(cli, "run_case", changed_manuscript)
    assert main(arguments(tmp_path / "run") + ["--case", "document"]) == 1


def test_budget_exhaustion_does_not_call_delegate():
    from unittest.mock import Mock

    case = cases("gpt-5.6-luna", "test")[0]
    delegate = Mock()
    budget = CallBudget(1)
    budget.consume()
    scenario = assemble(case, delegate, budget)
    result, _ = run_case(case, scenario)
    assert result.failure.code == "EVALUATION_CALL_BUDGET"
    delegate.generate.assert_not_called()


def test_saved_result_retry_never_repeats_model_call():
    case = cases("gpt-5.6-luna", "test")[0]
    budget = CallBudget(2)
    scenario = assemble(case, None, budget)
    scenario.calls.failures["publisher.ack"].append(
        RefreshFailure(Failure("ACK_TIMEOUT", "timeout", True))
    )
    with pytest.raises(RefreshFailure):
        run_case(case, scenario)
    assert budget.used == 1
    run_case(case, scenario)
    assert budget.used == 1


def test_live_requires_key_and_explicit_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        main(arguments(tmp_path / "run") + ["--live"])
    assert not (tmp_path / "run").exists()
    with pytest.raises(SystemExit):
        main(["--trial", "t", "--output", str(tmp_path / "other")])


def test_live_auth_failure_stops_suite_and_records_unknown_cost(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx2
    from openai import AuthenticationError

    import scripts.evaluate_refresh as cli

    create = Mock(
        side_effect=AuthenticationError(
            "private",
            body={"code": "invalid_api_key"},
            response=httpx2.Response(
                401, request=httpx2.Request("POST", "http://test")
            ),
        )
    )

    @contextmanager
    def fake_client(**kwargs):
        yield SimpleNamespace(max_retries=0, responses=SimpleNamespace(create=create))

    monkeypatch.setattr(cli, "OpenAI", fake_client)
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-value")
    target = tmp_path / "auth"
    assert main(arguments(target) + ["--live"]) == 1
    create.assert_called_once()
    record = json.loads((target / "document-attempt-1.json").read_text())
    assert record["sdk_version"] == "3.24.0"
    assert record["usage"] is None
    assert record["failure"]["code"] == "MODEL_AUTH_FAILED"
    markdown = (target / "report.md").read_text()
    assert "실행 실패" in markdown
    assert "미확인 | 미확인" in markdown
    assert "invalid_api_key" not in markdown
    assert not (target / "relation.json").exists()


@pytest.mark.parametrize("name", ["document", "relation", "both"])
def test_empty_model_fails_required_changes_and_matches_markdown(
    name, tmp_path, monkeypatch
):
    import scripts.evaluate_refresh as cli
    from app.refresh.models import ModelCandidate
    from evaluation.cases import evaluation_outcome
    from evaluation.markdown import status

    class EmptyModel:
        def generate(self, model_input):
            return ModelCandidate(())

    original_assemble = cli.assemble

    def assemble_empty(case, model, budget, recorder):
        return original_assemble(case, EmptyModel(), budget, recorder)

    monkeypatch.setattr(cli, "assemble", assemble_empty)
    output = tmp_path / name
    assert main(arguments(output) + ["--case", name]) == 1
    report = json.loads((output / f"{name}.json").read_text())
    assert report["result"]["outcome"] == "NO_CHANGE"
    assert evaluation_outcome(report) == "failed"
    assert status(report) == "검사 불일치"
    assert "검사 불일치" in (output / "report.md").read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("required_content_preserved", False),
        ("outdated_content_removed", False),
        ("preserved_content", False),
        ("expected_relations", False),
        ("unexpected_targets", ["unexpected"]),
        ("new_setting_content_valid", False),
    ],
)
def test_cli_fails_every_required_content_check(field, value, tmp_path, monkeypatch):
    import scripts.evaluate_refresh as cli

    original_run = cli.run_case

    def failed_assessment(*args, **kwargs):
        result, assessment = original_run(*args, **kwargs)
        assessment[field] = value
        return result, assessment

    monkeypatch.setattr(cli, "run_case", failed_assessment)
    output = tmp_path / field
    assert main(arguments(output) + ["--case", "document"]) == 1
    assert "검사 불일치" in (output / "report.md").read_text()


@pytest.mark.parametrize(
    "classification,code", [("allowed", 0), ("forbidden", 1), ("unreviewed", 2)]
)
def test_cli_and_markdown_distinguish_extra_creation_review(
    classification, code, tmp_path, monkeypatch, capsys
):
    from dataclasses import replace

    import scripts.evaluate_refresh as cli
    from evaluation.cases import new_setting_cases
    from evaluation.markdown import status

    case = new_setting_cases("gpt-5.6-luna", "test")[0]
    if classification != "unreviewed":
        case = replace(
            case, **{f"{classification}_new_settings": (("LOCATION", "항구"),)}
        )
    extra = replace(
        case.expected.new_document_proposals[0],
        candidate_id="new:LOCATION:항구",
        folder_code="LOCATION",
        name="항구",
        body_text="민수가 경비병으로 일하는 항구다.",
    )

    class ExtraCreationModel:
        def generate(self, model_input):
            return replace(
                case.expected,
                new_document_proposals=case.expected.new_document_proposals + (extra,),
            )

    original_assemble = cli.assemble

    def assemble_extra(case, model, budget, recorder):
        return original_assemble(case, ExtraCreationModel(), budget, recorder)

    monkeypatch.setattr(cli, "assemble", assemble_extra)
    monkeypatch.setattr(cli, "new_setting_cases", lambda model, trial: (case,))
    output = tmp_path / classification
    assert main(arguments(output) + ["--suite", "new-settings"]) == code
    summary = json.loads(capsys.readouterr().out)
    assert summary["failed"] == (classification == "forbidden")
    assert summary["review_required"] == (classification == "unreviewed")
    report = json.loads((output / f"{case.name}.json").read_text())
    expected_status = {
        "allowed": "자동 검사 통과",
        "forbidden": "검사 불일치",
        "unreviewed": "검토 필요",
    }[classification]
    assert status(report) == expected_status
    text = (output / "report.md").read_text()
    assert expected_status in text
    if classification == "unreviewed":
        assert "기준에 등록되지 않은 신규 설정 | 검토 필요: LOCATION:항구" in text
    assert "근거 E" not in text and "E0001" not in text
