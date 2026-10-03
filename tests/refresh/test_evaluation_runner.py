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
    assert report["result"]["execution"]["usage"]["calls"] == 2
    with pytest.raises(SystemExit):
        main(arguments(target))


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
