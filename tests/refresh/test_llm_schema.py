import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from app.adapters.llm_schema import candidate_schema, parse_candidate, response_usage
from app.refresh.errors import RefreshFailure
from app.refresh.models import Usage
from tests.fakes.real_rules import real_example


def payload():
    data = asdict(real_example()[-1])
    data.pop("usage")
    return json.loads(json.dumps(data))


@pytest.mark.parametrize(
    "documents,relations", [(True, False), (False, True), (True, True), (False, False)]
)
def test_candidate_variants(documents, relations):
    data = payload()
    if not documents:
        data["document_proposals"] = []
    if not relations:
        data["relation_proposals"] = []
    candidate = parse_candidate(json.dumps(data), Usage(1, 15, 20))
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
    data = payload()
    data["document_proposals"][0]["base_revision_no"] = value
    with pytest.raises(RefreshFailure):
        parse_candidate(json.dumps(data), Usage(1))


def test_nested_extra_field_and_invalid_json_fail():
    data = payload()
    data["document_proposals"][0]["evidence"][0]["unexpected"] = 1
    for text in (json.dumps(data), "{", "", "null"):
        with pytest.raises(RefreshFailure) as caught:
            parse_candidate(text, Usage(1))
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
