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
    assert not (target / "relation.json").exists()
