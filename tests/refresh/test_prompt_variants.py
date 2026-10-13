"""실험 버전이 입력/범위/출력 계약을 바꾸지 않는지 검증한다."""

import json
from dataclasses import asdict, replace

import pytest

from app.adapters.llm import OpenAIProposalModel
from app.refresh.models import ModelSettings
from app.refresh.policy import RELATION_KEYS
from app.refresh.prompt_versions import (
    BASELINE_PROMPT_VERSION,
    IMPROVED_PROMPT_VERSION,
)
from app.refresh.prompts import INSTRUCTION
from app.refresh.rules import RefreshRules
from tests.refresh.test_context import snapshot_fixture
from tests.refresh.test_llm import client_fixture, limits


def prompt(version):
    snapshot = snapshot_fixture()
    snapshot = replace(
        snapshot,
        request=replace(
            snapshot.request,
            model_settings=ModelSettings("gpt-6-luna", version, "refresh-candidate-v5"),
        ),
    )
    rules = RefreshRules()
    return rules.build_prompt(rules.build_context(snapshot, rules.chunk(snapshot)))


def test_baseline_remains_exact_and_variants_preserve_context_and_scope():
    baseline = prompt(BASELINE_PROMPT_VERSION)
    improved = prompt(IMPROVED_PROMPT_VERSION)
    assert baseline.instructions == (
        INSTRUCTION
        + "\nFOLDER_RELATION_KEYS="
        + json.dumps(RELATION_KEYS, sort_keys=True)
    )
    assert baseline.instructions != improved.instructions
    left, right = asdict(baseline), asdict(improved)
    for value in (left, right):
        value.pop("instructions")
        value.pop("prompt_version")
        context = json.loads(value.pop("prompt"))
        context["model_settings"].pop("prompt_version")
        value["context"] = context
    assert left == right


@pytest.mark.parametrize("version", [BASELINE_PROMPT_VERSION, IMPROVED_PROMPT_VERSION])
def test_both_registered_prompts_reach_responses_with_the_same_strict_schema(version):
    client = client_fixture()
    item = prompt(version)
    result = OpenAIProposalModel(client, limits("gpt-6-luna")).generate(item)
    assert result.usage.calls == 1
    params = client.responses.create.call_args.kwargs
    assert params["instructions"] == item.instructions
    assert params["text"]["format"]["strict"] is True
    assert params["truncation"] == "disabled"
    assert "seed" not in params
