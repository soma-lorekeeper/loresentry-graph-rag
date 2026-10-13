"""Repair feedback identifies the unchanged strict rule and exact candidate field."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from app.adapters.llm_schema import parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentProposal, Evidence, FactEntity, Outcome, Usage
from app.refresh.rules import RefreshRules
from tests.refresh.test_semantic_relations import parse, semantic_fixture


def parser_failure(payload, model_input):
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    usage = Usage(1, 17, 23)
    with pytest.raises(RefreshFailure) as caught:
        parse_candidate(
            text,
            usage,
            schema_version=model_input.schema_version,
            model_input=model_input,
        )
    error = caught.value
    assert error.failure.code == "MODEL_INVALID_RESPONSE"
    assert error.candidate_text == text
    assert error.usage == usage
    return error


@pytest.mark.parametrize(
    "defect,path,rule",
    [
        ("missing_action", "facts[0].action", "schema.missing"),
        ("extra_excerpt", "facts[0].source.excerpt", "schema.extra_forbidden"),
        ("unknown_marker", "facts[0].source.marker", "source.marker_known"),
        ("unknown_ref", "relation_proposals[0].fact_ids[0]", "fact.reference_known"),
        ("duplicate_ref", "relation_proposals[0].fact_ids", "fact.reference_unique"),
        ("duplicate_fact", "facts[1].fact_id", "fact.identifier_unique"),
    ],
)
def test_parser_feedback_preserves_wire_location_original_text_and_observed_usage(
    defect, path, rule
):
    payload, model_input, _ = semantic_fixture()
    marker = payload["facts"][0]["source"]["marker"]
    if defect == "missing_action":
        payload["facts"][0].pop("action")
    elif defect == "extra_excerpt":
        payload["facts"][0]["source"]["excerpt"] = "UNEXPECTED SOURCE COPY"
    elif defect == "unknown_marker":
        marker = payload["facts"][0]["source"]["marker"] = "unknown"
    elif defect == "unknown_ref":
        payload["relation_proposals"][0]["fact_ids"] = ["missing"]
    elif defect == "duplicate_ref":
        payload["relation_proposals"][0]["fact_ids"] *= 2
    else:
        payload["facts"].append(deepcopy(payload["facts"][0]))
    error = parser_failure(payload, model_input)
    diagnostic = error.diagnostics[0]
    assert diagnostic.path == path
    assert diagnostic.rule == rule
    assert diagnostic.fact_id == ("missing" if defect == "unknown_ref" else "F0001")
    if defect not in {"unknown_ref", "duplicate_ref"}:
        assert diagnostic.source_marker == marker
    assert "UNEXPECTED SOURCE COPY" not in str(error)


def test_malformed_json_retains_original_text_without_echoing_it_in_failure():
    text = '  {"facts": ["PRIVATE CANDIDATE TEXT"'
    usage = Usage(1, 7, 11)
    with pytest.raises(RefreshFailure) as caught:
        parse_candidate(text, usage)
    assert caught.value.candidate_text == text
    assert caught.value.usage == usage
    assert caught.value.diagnostics[0].path == "$"
    assert caught.value.diagnostics[0].rule == "schema.json_invalid"
    assert caught.value.diagnostics[0].offending_value is None
    assert "PRIVATE CANDIDATE TEXT" not in str(caught.value)


def test_server_context_failure_preserves_usage_but_has_no_candidate_repair_feedback():
    payload, model_input, _ = semantic_fixture()
    context = json.loads(model_input.prompt)
    context["evidence"].append(context["evidence"][0])
    error = parser_failure(
        payload, replace(model_input, prompt=json.dumps(context, ensure_ascii=False))
    )
    assert error.diagnostics == ()


def test_legacy_v4_invalid_excerpt_has_precise_source_feedback_without_relaxing_reader():
    payload, model_input, _ = semantic_fixture(schema_version="refresh-candidate-v4")
    payload["facts"][0]["source"]["excerpt"] = "원문에 없는 발췌"
    error = parser_failure(payload, model_input)
    assert error.diagnostics[0].path == "facts[0].source.excerpt"
    assert error.diagnostics[0].rule == "source.excerpt_exact"


@pytest.mark.parametrize(
    "field,value,path,rule",
    [
        ("action", "원문에 없는 행동", "facts[0].action", "fact.action_in_source"),
        (
            "actor.surface",
            "다른 사람",
            "facts[0].actor.surface",
            "fact.role_surface_in_source",
        ),
        (
            "recipient.surface",
            "다른 사람",
            "facts[0].recipient.surface",
            "fact.role_surface_in_source",
        ),
        (
            "object.surface",
            "다른 물건",
            "facts[0].object.surface",
            "fact.role_surface_in_source",
        ),
        (
            "actor.document_id",
            "yunjun",
            "facts[0].actor.document_id",
            "fact.entity_identity_matches",
        ),
        (
            "recipient.document_id",
            "yunjun",
            "facts[0].recipient.document_id",
            "fact.entity_identity_matches",
        ),
        (
            "statement",
            "방문 전달을 맡겼다.",
            "facts[0].actor.surface",
            "fact.role_surface_in_statement",
        ),
    ],
)
def test_validation_feedback_names_typed_fact_index_canonical_id_source_and_field(
    field, value, path, rule
):
    payload, model_input, snapshot = semantic_fixture()
    if "." in field:
        role, key = field.split(".")
        payload["facts"][0][role][key] = value
    else:
        payload["facts"][0][field] = value
    candidate = parse(payload, model_input)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_FACT"
    assert result.usage == candidate.usage
    assert not result.facts and not result.relation_proposals
    diagnostic = result.diagnostics[0]
    assert diagnostic.path == path
    assert diagnostic.rule == rule
    assert diagnostic.fact_id == candidate.facts[0].fact_id
    assert diagnostic.source_marker == payload["facts"][0]["source"]["marker"]
    assert diagnostic.offending_value == (
        candidate.facts[0].actor.surface if field == "statement" else value
    )


@pytest.mark.parametrize("defect", ["narrow_source", "wrong_original"])
def test_typed_source_failure_has_feedback_and_still_fails_all_proposals(defect):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    source = candidate.facts[0].evidence[0]
    if defect == "narrow_source":
        quote = payload["facts"][0]["statement"]
        start = source.quote.index(quote)
        source = replace(source, start=start, end=start + len(quote), quote=quote)
        expected = ("INVALID_FACT", "source.server_chunk", "facts[0].source.marker")
    else:
        source = replace(source, quote="다른 원문")
        expected = ("INVALID_EVIDENCE", "source.original_matches", "facts[0].source")
    candidate = replace(
        candidate, facts=(replace(candidate.facts[0], evidence=(source,)),)
    )
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.FAILED
    assert (
        result.failure.code,
        result.diagnostics[0].rule,
        result.diagnostics[0].path,
    ) == expected
    assert result.diagnostics[0].fact_id == candidate.facts[0].fact_id
    assert not result.relation_proposals


@pytest.mark.parametrize(
    "refs,rule",
    [((), "fact.references_nonempty"), (("unknown",), "fact.reference_known")],
)
def test_typed_unknown_and_empty_references_keep_specific_proposal_feedback(refs, rule):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    candidate = replace(
        candidate,
        relation_proposals=(replace(candidate.relation_proposals[0], fact_ids=refs),),
    )
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.failure.code == "INVALID_FACT_REFERENCE"
    assert result.diagnostics[0].path == (
        "relation_proposals[0].fact_ids[0]"
        if refs
        else "relation_proposals[0].fact_ids"
    )
    assert result.diagnostics[0].rule == rule


def test_fact_reference_outside_document_evidence_has_specific_coverage_feedback():
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    document = snapshot.documents[1]
    candidate = replace(
        candidate,
        document_proposals=(
            DocumentProposal(
                document.document_id,
                document.revision_no,
                "body_text",
                "새 설정 본문",
                (
                    Evidence(
                        document.document_id,
                        document.revision_no,
                        0,
                        len(document.body_text),
                        document.body_text,
                    ),
                ),
                (candidate.facts[0].fact_id,),
            ),
        ),
    )
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.failure.code == "INVALID_FACT_REFERENCE"
    diagnostic = result.diagnostics[0]
    assert diagnostic.path == "document_proposals[0].fact_ids[0]"
    assert diagnostic.rule == "fact.reference_source_coverage"
    assert diagnostic.fact_id == candidate.facts[0].fact_id
    assert diagnostic.source_marker == payload["facts"][0]["source"]["marker"]


def test_success_keeps_canonical_candidate_and_has_no_failure_diagnostics():
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.facts == candidate.facts
    assert result.diagnostics == ()


def test_safe_leaf_failures_collect_all_facts_and_roles_before_proposal_checks():
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    original = candidate.facts[0]
    first = replace(
        original,
        action="원문에 없는 행동",
        actor=FactEntity("보이지 않는 행위자", "haeju"),
        recipient=replace(original.recipient, document_id="yunjun"),
        object=FactEntity("보이지 않는 물건", None),
    )
    second = replace(
        original,
        fact_id="second-fact",
        actor=replace(original.actor, document_id="missing-actor"),
        recipient=replace(original.recipient, document_id="missing-recipient"),
        object=replace(original.object, document_id="missing-object"),
    )
    # A bad proposal ref must not obscure the independent fact diagnostics.
    candidate = replace(
        candidate,
        facts=(first, second),
        relation_proposals=(
            replace(candidate.relation_proposals[0], fact_ids=("missing-fact",)),
        ),
    )
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_FACT"
    assert result.failure.message == "Fact action must preserve source wording"
    assert not result.facts and not result.relation_proposals
    assert result.usage == candidate.usage
    assert [(d.path, d.rule) for d in result.diagnostics] == [
        ("facts[0].action", "fact.action_in_source"),
        ("facts[0].actor.surface", "fact.role_surface_in_source"),
        ("facts[0].actor.surface", "fact.role_surface_in_statement"),
        ("facts[0].actor.document_id", "fact.entity_identity_matches"),
        ("facts[0].recipient.document_id", "fact.entity_identity_matches"),
        ("facts[0].object.surface", "fact.role_surface_in_source"),
        ("facts[0].object.surface", "fact.role_surface_in_statement"),
        ("facts[1].actor.document_id", "fact.entity_reference_known"),
        ("facts[1].recipient.document_id", "fact.entity_reference_known"),
        ("facts[1].object.document_id", "fact.entity_reference_known"),
    ]
    assert {d.fact_id for d in result.diagnostics[:7]} == {first.fact_id}
    assert {d.fact_id for d in result.diagnostics[7:]} == {second.fact_id}
    assert {d.source_marker for d in result.diagnostics} == {
        payload["facts"][0]["source"]["marker"]
    }


@pytest.mark.parametrize("document_id", ["unknown", 17, ["unhashable"]])
def test_invalid_entity_ids_reject_every_role_without_looking_up_unknown_names(
    document_id,
):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    fact = candidate.facts[0]
    fact = replace(
        fact,
        **{
            role: replace(getattr(fact, role), document_id=document_id)
            for role in ("actor", "recipient", "object")
        },
    )
    result = RefreshRules().validate_candidate(
        snapshot, replace(candidate, facts=(fact,)), model_input
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_FACT"
    assert [(d.path, d.rule) for d in result.diagnostics] == [
        (f"facts[0].{role}.document_id", "fact.entity_reference_known")
        for role in ("actor", "recipient", "object")
    ]
    assert not result.facts and not result.relation_proposals


@pytest.mark.parametrize(
    "defect,path,rule",
    [
        ("fact_type", "facts[1]", "fact.typed"),
        ("role_type", "facts[1].actor", "fact.role_typed"),
        ("surface_type", "facts[1].actor.surface", "fact.role_surface_bounded"),
        ("status", "facts[1].certainty", "fact.certainty_known"),
        ("source", "facts[1].source", "source.original_matches"),
    ],
)
def test_unsafe_fact_stops_collection_and_preserves_the_first_leaf_failure(
    defect, path, rule
):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    original = candidate.facts[0]
    first = replace(original, action="원문에 없는 행동")
    second = replace(original, fact_id="second-fact")
    if defect == "fact_type":
        second = object()
    elif defect == "role_type":
        second = replace(second, actor=object())
    elif defect == "surface_type":
        second = replace(second, actor=replace(second.actor, surface=["wrong"]))
    elif defect == "status":
        second = replace(second, certainty="UNSUPPORTED")
    else:
        second = replace(
            second, evidence=(replace(second.evidence[0], quote="다른 원문"),)
        )
    third = replace(original, fact_id="unchecked-fact", action="검사하지 않는 행동")
    result = RefreshRules().validate_candidate(
        snapshot, replace(candidate, facts=(first, second, third)), model_input
    )
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == "INVALID_FACT"
    assert result.failure.message == "Fact action must preserve source wording"
    assert [(d.path, d.rule) for d in result.diagnostics] == [
        ("facts[0].action", "fact.action_in_source"),
        (path, rule),
    ]
    assert not result.facts and not result.relation_proposals
