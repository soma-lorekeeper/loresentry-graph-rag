"""실험 버전이 입력/범위/출력 계약을 바꾸지 않는지 검증한다."""

import json
import re
from dataclasses import asdict, replace

import pytest

from app.adapters.llm import OpenAIProposalModel
from app.adapters.llm_schema import parse_candidate
from app.refresh.models import (
    DocumentSnapshot,
    ModelSettings,
    Outcome,
    RelatedDocuments,
    Relation,
    SelectionSettings,
    Usage,
)
from app.refresh.policy import RELATION_KEYS
from app.refresh.prompt_versions import (
    BASELINE_PROMPT_VERSION,
    IMPROVED_PROMPT_VERSION,
)
from app.refresh.prompts import INSTRUCTION, JUDGMENT_EXAMPLES, build_prompt
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


@pytest.mark.parametrize("version", [BASELINE_PROMPT_VERSION, IMPROVED_PROMPT_VERSION])
@pytest.mark.parametrize("example_index", [0, 1])
def test_instruction_examples_pass_live_candidate_and_source_validation(
    version, example_index
):
    """지시문의 정답 자체를 실제 스키마·출처·제안 검증에 통과시킨다."""
    blocks = re.findall(r"```json\n(.*?)\n```", JUDGMENT_EXAMPLES, re.DOTALL)
    assert len(blocks) == 4
    context = json.loads(blocks[2 * example_index])
    output = blocks[2 * example_index + 1]
    original = snapshot_fixture()
    settings = ModelSettings("example-model", version, "refresh-candidate-v5")
    request = replace(
        original.request,
        model_settings=settings,
        settings=SelectionSettings(),
    )
    documents = tuple(
        DocumentSnapshot(
            project_id=request.job.project_id,
            document_id=document["document_id"],
            revision_no=document["revision_no"],
            body_text=next(
                source["quote"]
                for source in context["evidence"]
                if source["document_id"] == document["document_id"]
            ),
            folder_code=document["folder_code"],
            title=document.get("title"),
            properties=tuple(document["properties"].items()),
        )
        for document in context["documents"]
    )
    snapshot = replace(
        original,
        request=request,
        documents=documents,
        related=RelatedDocuments(
            relations=tuple(
                Relation(project_id=request.job.project_id, **relation)
                for relation in context["relations"]
            )
        ),
        target_ids=tuple(context["target_ids"]),
        changed_ids=tuple(context["changed_ids"]),
    )
    context["model_settings"] = asdict(settings)
    context["max_context_chars"] = request.settings.max_context_chars
    model_input = build_prompt(json.dumps(context, ensure_ascii=False))
    candidate = parse_candidate(output, Usage(), model_input=model_input)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED, (result.failure, result.diagnostics)
    assert result.facts == candidate.facts
    assert len(result.document_proposals) == len(candidate.document_proposals)
    assert len(result.new_document_proposals) == len(candidate.new_document_proposals)
    assert len(result.relation_proposals) == len(candidate.relation_proposals)
