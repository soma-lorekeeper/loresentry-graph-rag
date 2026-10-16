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


def test_semantic_cli_uses_current_contract_without_exposing_fact_refs(
    tmp_path, capsys
):
    target = tmp_path / "semantic"
    assert (
        main(
            arguments(target)
            + ["--suite", "semantic", "--max-calls", "16", "--repeat-saved"]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["cases"] == 6 and summary["calls"] == 16
    markdown = (target / "report.md").read_text()
    assert "역할·수령인·범위 검사는 등록된 사실 기준을 비교합니다" in markdown
    for path in target.glob("*.json"):
        if "-attempt-" in path.stem:
            continue
        report = json.loads(path.read_text())
        assert report["result"]["execution"]["candidate_schema_version"] == (
            "refresh-candidate-v5"
        )
        semantic = report["assessment"]["semantic_assessment"]
        assert semantic["structured_criteria"] == "matched"
        assert semantic["criteria_count"] > 0
        assert semantic["source_entailment"] == "human_review_pending"
        assert semantic["free_text_bodies"] == "human_review_pending"
        assert report["result"]["facts"]
    assert "evidence_refs" not in markdown and "fact_id" not in markdown


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
    assert result.usage.calls == 0 and not result.attempts
    delegate.generate.assert_not_called()


def test_saved_result_retry_never_repeats_model_call():
    case = cases("gpt-5.6-luna", "test")[0]
    budget = CallBudget(2)
    scenario = assemble(case, None, budget)
    scenario.calls.failures["inbox.ack"].append(
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
    # This test supplies an old golden response, rather than a current v4 model.
    case = replace(
        case,
        request=replace(
            case.request,
            model_settings=replace(
                case.request.model_settings,
                schema_version="refresh-candidate-v3",
                prompt_version="refresh-prompt-v7",
            ),
        ),
    )
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


@pytest.mark.parametrize("fixed,maximum", [(False, 4), (True, 4), (True, 3)])
def test_cli_records_validation_repair_and_saved_reuse_with_shared_call_budget(
    tmp_path, monkeypatch, capsys, fixed, maximum
):
    from dataclasses import replace

    import scripts.evaluate_refresh as cli
    from evaluation.runner import EvaluationModel
    from evaluation.semantic_cases import semantic_cases

    case = semantic_cases("gpt-5.6-luna", "test")[0]
    baseline = EvaluationModel(case, None, CallBudget(20))

    class InvalidInitialModel:
        def __init__(self):
            self.count = 0

        def generate(self, model_input):
            candidate = baseline._generate(model_input)
            self.count += 1
            if self.count == 1 or not fixed:
                fact = candidate.facts[0]
                candidate = replace(
                    candidate,
                    facts=(
                        replace(
                            fact,
                            actor=replace(fact.actor, document_id="unknown-character"),
                        ),
                    )
                    + candidate.facts[1:],
                )
            return candidate

    delegate = InvalidInitialModel()
    original_assemble = cli.assemble

    def assemble_scripted(case, model, budget, recorder):
        return original_assemble(case, delegate, budget, recorder)

    monkeypatch.setattr(cli, "assemble", assemble_scripted)
    monkeypatch.setattr(cli, "semantic_cases", lambda model, trial: (case,))
    output = tmp_path / "repair"
    succeeded = fixed and maximum == 4
    calls = maximum if fixed else 3
    assert main(
        arguments(output)
        + ["--suite", "semantic", "--max-calls", str(maximum), "--repeat-saved"]
    ) == (0 if succeeded else 1)
    summary = json.loads(capsys.readouterr().out)
    assert summary["calls"] == calls
    repairs = 1 if fixed else 2
    assert summary["repair_attempts"] == repairs
    assert summary["repair_successes"] == int(fixed)
    assert delegate.count == summary["calls"]
    report = json.loads((output / "semantic-roles.json").read_text())
    assert report["attempts"] == summary["calls"]
    assert report["result"]["execution"]["usage"]["calls"] == summary["calls"]
    records = [
        json.loads(path.read_text()) for path in sorted(output.glob("*-attempt-*.json"))
    ]
    assert len(records) == summary["calls"]
    assert [record["attempt_index"] for record in records[:2]] == [0, 1]
    assert [record["call_index"] for record in records[:2]] == [1, 1]
    assert records[0]["validation"]["outcome"] == "FAILED"
    assert records[1]["validation"]["outcome"] == ("PROPOSED" if fixed else "FAILED")
    text = (output / "report.md").read_text()
    assert f"| 보완 시도 | {repairs} |" in text
    assert f"| 보완 성공 | {int(fixed)} |" in text
    assert (
        f"| 최종 채택 수정 / 신규 설정 / 관계 | {'3 / 0 / 2' if succeeded else '0 / 0 / 0'} |"
        in text
    )
    counts = f"{calls} / 0 / 3"
    assert f"| 호출별 후보 수(중복 포함) | {counts} |" in text
    assert "evidence_refs" not in text and "source_marker" not in text
