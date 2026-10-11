import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from app.adapters.llm_schema import (
    CANDIDATE_VERSION,
    PROMPT_VERSION,
    candidate_schema,
    parse_candidate,
    response_usage,
)
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentBatch, Evidence, ModelInput, Outcome, Usage
from app.refresh.rules import RefreshRules
from tests.fakes.real_rules import real_example


def legacy_payload():
    data = asdict(real_example()[-1])
    data.pop("usage")
    data.pop("facts")
    for proposals in data.values():
        for proposal in proposals:
            proposal.pop("fact_ids")
    return json.loads(json.dumps(data))


def source_fact(source, *, marker="C0001", actor=None, action="왔다"):
    return {
        "fact_id": "F1",
        "actor": {"surface": actor, "document_id": None} if actor else None,
        "action": action,
        "recipient": None,
        "object": None,
        "certainty": "CONFIRMED",
        "extent": "UNSPECIFIED",
        "time": "UNSPECIFIED",
        "statement": source.quote,
        "source": {"marker": marker},
    }


def marker_fixture():
    request, source, related, targets, _ = real_example()
    rules = RefreshRules()
    snapshot = rules.assemble(
        request,
        source,
        related,
        rules.select(request, source, related),
        DocumentBatch(targets),
    )
    model_input = rules.build_prompt(
        rules.build_context(snapshot, rules.chunk(snapshot))
    )
    context = json.loads(model_input.prompt)
    data = legacy_payload()
    for proposals in data.values():
        for proposal in proposals:
            proposal["evidence"] = [
                {"marker": item["marker"]}
                for evidence in proposal["evidence"]
                for item in context["evidence"]
                if evidence
                == {key: value for key, value in item.items() if key != "marker"}
            ]
    return data, model_input, snapshot


@pytest.mark.parametrize(
    "documents,relations", [(True, False), (False, True), (True, True), (False, False)]
)
def test_candidate_variants(documents, relations):
    data, model_input, _ = marker_fixture()
    if not documents:
        data["document_proposals"] = []
    if not relations:
        data["relation_proposals"] = []
    candidate = parse_candidate(
        json.dumps(data),
        Usage(1, 15, 20),
        schema_version="refresh-candidate-v3",
        model_input=model_input,
    )
    assert bool(candidate.document_proposals) == documents
    assert bool(candidate.relation_proposals) == relations
    assert candidate.usage == Usage(1, 15, 20)
    with pytest.raises(FrozenInstanceError):
        candidate.document_proposals = ()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"document_proposals": []},
        {"document_proposals": [], "relation_proposals": [], "extra": 1},
    ],
)
def test_missing_or_extra_fields_fail(bad):
    with pytest.raises(RefreshFailure):
        parse_candidate(json.dumps(bad), Usage(1))


@pytest.mark.parametrize("value", ["3", 3.0, True, None])
def test_revision_never_coerced(value):
    data, model_input, _ = marker_fixture()
    data["document_proposals"][0]["base_revision_no"] = value
    with pytest.raises(RefreshFailure):
        parse_candidate(
            json.dumps(data),
            Usage(1),
            schema_version="refresh-candidate-v3",
            model_input=model_input,
        )


def test_nested_extra_field_and_invalid_json_fail():
    data, model_input, _ = marker_fixture()
    data["document_proposals"][0]["evidence"][0]["unexpected"] = 1
    for text in (json.dumps(data), "{", "", "null"):
        with pytest.raises(RefreshFailure) as caught:
            parse_candidate(
                text,
                Usage(1),
                schema_version="refresh-candidate-v3",
                model_input=model_input,
            )
        assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


def test_usage_never_double_counts_details():
    assert response_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 90},
            "output_tokens_details": {"reasoning_tokens": 40},
        }
    ) == Usage(1, 100, 50)


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"input_tokens": 0},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": -1, "output_tokens": 2},
        {"input_tokens": 1, "output_tokens": "2"},
    ],
)
def test_unknown_usage_is_not_zero_cost(data):
    with pytest.raises(RefreshFailure):
        response_usage(data)


def test_schema_requires_every_field_and_disallows_extras():
    schema = candidate_schema()
    for obj in [schema, *schema["$defs"].values()]:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert schema["$defs"]["EvidenceMarkerDTO"]["properties"] == {
        "marker": {"title": "Marker", "type": "string"}
    }
    assert "EvidenceDTO" not in schema["$defs"]


def test_marker_hydration_preserves_unicode_source_and_passes_unchanged_rules():
    data, model_input, snapshot = marker_fixture()
    candidate = parse_candidate(
        json.dumps(data),
        Usage(1),
        schema_version="refresh-candidate-v3",
        model_input=model_input,
    )
    assert candidate.document_proposals == real_example()[-1].document_proposals
    assert candidate.relation_proposals == real_example()[-1].relation_proposals
    assert "😀" in candidate.document_proposals[0].evidence[0].quote
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.failure is None


def test_repeated_quote_markers_select_exact_occurrence_without_text_search():
    data, model_input, snapshot = marker_fixture()
    source = real_example()[1].documents[1]
    quote = "같은 말."
    first_start = source.body_text.index(quote)
    second_start = source.body_text.index(quote, first_start + len(quote))
    first = Evidence("m2", 5, first_start, first_start + len(quote), quote)
    second = Evidence("m2", 5, second_start, second_start + len(quote), quote)
    model_input = replace(
        model_input,
        prompt=json.dumps(
            {
                "evidence": [
                    {"marker": "C0001", **asdict(first)},
                    {"marker": "C0002", **asdict(second)},
                ]
            }
        ),
    )
    data["document_proposals"] = data["document_proposals"][:1]
    data["relation_proposals"] = []
    data["document_proposals"][0]["evidence"] = [{"marker": "C0002"}]
    for _ in range(2):
        candidate = parse_candidate(
            json.dumps(data),
            Usage(1),
            schema_version="refresh-candidate-v3",
            model_input=model_input,
        )
        assert candidate.document_proposals[0].evidence == (second,)
        assert (
            RefreshRules().validate_candidate(snapshot, candidate, model_input).outcome
            == Outcome.PROPOSED
        )


@pytest.mark.parametrize(
    "evidence",
    [
        [{"marker": "unknown"}],
        [{"marker": ""}],
        [{"marker": 1}],
        [{"marker": "C0001", "quote": "원문 수정"}],
        [{"marker": "C0001", "start": 0, "end": 1}],
        [{"document_id": "m1", "revision_no": 3, "start": 0, "end": 1, "quote": "유"}],
    ],
)
def test_unknown_or_manual_evidence_rejected(evidence):
    data, model_input, _ = marker_fixture()
    data["document_proposals"][0]["evidence"] = evidence
    with pytest.raises(RefreshFailure) as caught:
        parse_candidate(
            json.dumps(data),
            Usage(1),
            schema_version="refresh-candidate-v3",
            model_input=model_input,
        )
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "defect", ["duplicate", "extra", "coerced", "missing", "invalid_json"]
)
def test_corrupt_context_never_resolves_markers(defect):
    data, model_input, _ = marker_fixture()
    context = json.loads(model_input.prompt)
    if defect == "duplicate":
        context["evidence"].append(context["evidence"][0])
    elif defect == "extra":
        context["evidence"][0]["unexpected"] = "private data"
    elif defect == "coerced":
        context["evidence"][0]["start"] = "0"
    elif defect == "missing":
        context.pop("evidence")
    model_input = replace(
        model_input, prompt="{" if defect == "invalid_json" else json.dumps(context)
    )
    with pytest.raises(RefreshFailure) as caught:
        parse_candidate(
            json.dumps(data),
            Usage(1),
            schema_version="refresh-candidate-v3",
            model_input=model_input,
        )
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"
    assert "private data" not in str(caught.value)


def test_markers_require_call_context_but_empty_candidate_can_be_read_without_it():
    data, _, _ = marker_fixture()
    with pytest.raises(RefreshFailure):
        parse_candidate(
            json.dumps(data), Usage(1), schema_version="refresh-candidate-v3"
        )
    assert (
        parse_candidate(
            json.dumps({name: [] for name in data}),
            Usage(1),
            schema_version="refresh-candidate-v3",
        ).document_proposals
        == ()
    )


@pytest.mark.parametrize("version", ["refresh-candidate-v1", "refresh-candidate-v2"])
def test_original_source_evidence_remains_readable_for_saved_legacy_candidates(version):
    data = legacy_payload()
    if version.endswith("v1"):
        data.pop("new_document_proposals")
    candidate = parse_candidate(json.dumps(data), Usage(1), schema_version=version)
    assert candidate.document_proposals == real_example()[-1].document_proposals
    assert candidate.relation_proposals == real_example()[-1].relation_proposals


def test_new_setting_marker_hydration_uses_same_trusted_context():
    quote = "서하 🐈 왔다"
    source = Evidence("chapter", 2, 2, 2 + len(quote), quote)
    model_input = ModelInput(
        json.dumps({"evidence": [{"marker": "C0001", **asdict(source)}]}),
        CANDIDATE_VERSION,
        PROMPT_VERSION,
        "gpt-5.6-luna",
    )
    candidate = parse_candidate(
        json.dumps(
            {
                "facts": [source_fact(source, actor="서하")],
                "document_proposals": [],
                "relation_proposals": [],
                "new_document_proposals": [
                    {
                        "candidate_id": "new:CHARACTER:서하",
                        "folder_code": "CHARACTER",
                        "name": "서하",
                        "body_text": "서하가 왔다.",
                        "evidence": [{"marker": "C0001"}],
                        "fact_ids": ["F1"],
                    }
                ],
            }
        ),
        Usage(1),
        model_input=model_input,
    )
    assert candidate.new_document_proposals[0].evidence == (source,)
    assert candidate.new_document_proposals[0].fact_ids == (candidate.facts[0].fact_id,)
    assert candidate.facts[0].evidence == (source,)
