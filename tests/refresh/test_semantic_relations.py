"""Fact grounding binds identities and references; it does not prove entailment."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from app.adapters.llm_schema import parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    ArtifactRef,
    DocumentProposal,
    DocumentSnapshot,
    Evidence,
    ExecutionSnapshot,
    FactEntity,
    JobKey,
    JobRecord,
    ModelCandidate,
    ModelInput,
    ModelSettings,
    Outcome,
    RefreshRequest,
    RelatedDocuments,
    RelationGenerationScope,
    Usage,
)
from app.refresh.rules import RefreshRules
from app.refresh.service import RefreshService

NAMED_EXCERPT = "민해주는 백도윤에게 방문 전달을 맡겼다."
ANONYMOUS_EXCERPT = "반환 담당자는 어머니에게 남은 기억 일부를 돌려주었다."


def semantic_fixture(excerpt=NAMED_EXCERPT, *, schema_version="refresh-candidate-v5"):
    text = f"기록 앞부분 🐈.\n{excerpt}\n윤준은 어머니를 기다렸다.\n기록 뒷부분."
    request = RefreshRequest(
        JobKey("semantic-project", "request", "fingerprint"),
        ArtifactRef("bucket", "input"),
        ("chapter",),
        target_ids=("haeju", "doyun", "yunjun"),
        discover_related=False,
        model_settings=ModelSettings(schema_version=schema_version),
    )
    documents = (
        DocumentSnapshot("semantic-project", "chapter", 2, text),
        DocumentSnapshot(
            "semantic-project",
            "haeju",
            3,
            "민해주는 기억 반환소를 맡는다. 회색 머리와 목덜미의 화상 자국이 있다.",
            properties=(("name", "민해주"), ("alias", "해주")),
            folder_code="CHARACTER",
            title="민해주",
        ),
        DocumentSnapshot(
            "semantic-project",
            "doyun",
            4,
            "백도윤은 배달부다.",
            properties=(("name", "백도윤"), ("alias", "도윤")),
            folder_code="CHARACTER",
            title="백도윤",
        ),
        DocumentSnapshot(
            "semantic-project",
            "yunjun",
            1,
            "윤준은 살아 있으며 어머니에게 남은 기억 일부가 반환되었다.",
            properties=(("name", "윤준"), ("alias", "준")),
            folder_code="CHARACTER",
            title="윤준",
        ),
    )
    snapshot = ExecutionSnapshot(
        request, documents, RelatedDocuments(), request.target_ids, ("chapter",)
    )
    rules = RefreshRules()
    context = json.loads(rules.build_context(snapshot, rules.chunk(snapshot)))
    marker = next(
        item["marker"]
        for item in context["evidence"]
        if item["document_id"] == "chapter"
    )
    model_input = ModelInput(
        json.dumps(context, ensure_ascii=False),
        schema_version,
        request.model_settings.prompt_version,
        "fake",
        target_ids=request.target_ids,
    )
    payload = {
        "facts": [
            {
                "fact_id": "F0001",
                "actor": {"surface": "민해주", "document_id": "haeju"},
                "action": "맡겼다",
                "recipient": {"surface": "백도윤", "document_id": "doyun"},
                "object": {"surface": "방문 전달", "document_id": None},
                "certainty": "CONFIRMED",
                "extent": "UNSPECIFIED",
                "time": "PAST",
                "statement": NAMED_EXCERPT,
                "source": (
                    {"marker": marker, "excerpt": excerpt}
                    if schema_version == "refresh-candidate-v4"
                    else {"marker": marker}
                ),
            }
        ],
        "document_proposals": [],
        "new_document_proposals": [],
        "relation_proposals": [
            {
                "document_id": "haeju",
                "target_document_id": "doyun",
                "base_revision_no": 3,
                "target_base_revision_no": 4,
                "relation_key": "related_character",
                "reverse_relation_key": "related_character",
                "operation": "ADD",
                "fact_ids": ["F0001"],
            }
        ],
    }
    return payload, model_input, snapshot


def parse(payload, model_input):
    return parse_candidate(
        json.dumps(payload, ensure_ascii=False),
        Usage(1, 20, 30),
        schema_version=model_input.schema_version,
        model_input=model_input,
    )


def checked(payload, model_input, snapshot):
    return RefreshRules().validate_candidate(
        snapshot, parse(payload, model_input), model_input
    )


def unused_fact_payload(payload):
    fact = deepcopy(payload["facts"][0])
    fact.update(
        fact_id="unused",
        actor={"surface": "윤준", "document_id": "yunjun"},
        action="기다렸다",
        recipient=None,
        object={"surface": "어머니", "document_id": None},
        statement="윤준은 어머니를 기다렸다.",
    )
    return fact


def assert_failed(result, code):
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == code
    assert not result.document_proposals
    assert not result.new_document_proposals
    assert not result.relation_proposals
    assert not result.facts


def test_named_fact_derives_relation_and_restores_exact_full_unicode_marker():
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    fact = candidate.facts[0]
    source = snapshot.documents[0]
    assert fact.evidence == (
        Evidence("chapter", 2, 0, len(source.body_text), source.body_text),
    )
    assert fact.actor == FactEntity("민해주", "haeju")
    assert fact.recipient == FactEntity("백도윤", "doyun")
    assert candidate.relation_proposals[0].description == fact.statement
    assert candidate.relation_proposals[0].evidence == fact.evidence
    assert candidate.relation_proposals[0].fact_ids == (fact.fact_id,)
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.facts == candidate.facts


@pytest.mark.parametrize("defect", ["unknown_marker", "wrong_excerpt", "empty_excerpt"])
def test_v4_fact_source_must_resolve_to_exact_supplied_excerpt(defect):
    payload, model_input, _ = semantic_fixture(schema_version="refresh-candidate-v4")
    source = payload["facts"][0]["source"]
    if defect == "unknown_marker":
        source["marker"] = "unknown"
    else:
        source["excerpt"] = "" if defect == "empty_excerpt" else "민해주는 승인했다."
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("text,excerpt", [("가가가", "가가"), ("가가 가가", "가가")])
def test_v4_fact_source_rejects_overlapping_and_separate_ambiguous_occurrences(
    text, excerpt
):
    payload, model_input, _ = semantic_fixture(
        text, schema_version="refresh-candidate-v4"
    )
    payload["facts"][0]["source"]["excerpt"] = excerpt
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


def test_v4_fact_excerpt_reader_preserves_exact_short_span():
    payload, model_input, snapshot = semantic_fixture(
        schema_version="refresh-candidate-v4"
    )
    candidate = parse(payload, model_input)
    source = snapshot.documents[0]
    start = source.body_text.index(NAMED_EXCERPT)
    assert candidate.facts[0].evidence == (
        Evidence("chapter", 2, start, start + len(NAMED_EXCERPT), NAMED_EXCERPT),
    )
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.candidate_schema_version == "refresh-candidate-v4"


def test_v4_fact_excerpt_reader_retains_500_character_limit():
    payload, model_input, _ = semantic_fixture(
        "가" * 501, schema_version="refresh-candidate-v4"
    )
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("defect", ["unknown_marker", "excerpt"])
def test_v5_fact_source_accepts_only_a_known_marker(defect):
    payload, model_input, _ = semantic_fixture()
    if defect == "unknown_marker":
        payload["facts"][0]["source"]["marker"] = "unknown"
    else:
        payload["facts"][0]["source"]["excerpt"] = NAMED_EXCERPT
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "text", [NAMED_EXCERPT + "\n" + NAMED_EXCERPT, NAMED_EXCERPT + "\n" + "기록 " * 180]
)
def test_v5_fact_marker_does_not_require_unique_or_short_excerpt(text):
    payload, model_input, snapshot = semantic_fixture(text)
    result = checked(payload, model_input, snapshot)
    assert result.outcome == Outcome.PROPOSED
    source = snapshot.documents[0]
    assert result.facts[0].evidence == (
        Evidence("chapter", 2, 0, len(source.body_text), source.body_text),
    )
    assert len(result.facts[0].statement) <= 500


@pytest.mark.parametrize("kind", ["document", "new_document", "relation"])
@pytest.mark.parametrize("reference", ["unknown", "canonical"])
def test_wire_fact_reference_requires_a_declared_local_identifier(kind, reference):
    payload, model_input, snapshot = semantic_fixture()
    invalid_reference = (
        parse(payload, model_input).facts[0].fact_id
        if reference == "canonical"
        else "unknown"
    )
    marker = payload["facts"][0]["source"]["marker"]
    if kind == "document":
        payload["document_proposals"] = [
            {
                "target_document_id": "haeju",
                "base_revision_no": 3,
                "field": "body_text",
                "value": snapshot.documents[1].body_text + " " + NAMED_EXCERPT,
                "evidence": [{"marker": marker}],
                "fact_ids": [invalid_reference],
            }
        ]
    elif kind == "new_document":
        payload["new_document_proposals"] = [
            {
                "candidate_id": "new:EVENT:방문 전달 배정",
                "folder_code": "EVENT",
                "name": "방문 전달 배정",
                "body_text": NAMED_EXCERPT,
                "evidence": [{"marker": marker}],
                "fact_ids": [invalid_reference],
            }
        ]
    else:
        payload["relation_proposals"][0]["fact_ids"] = [invalid_reference]
    assert invalid_reference != payload["facts"][0]["fact_id"]
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("field", ["actor", "recipient", "object"])
def test_role_surface_missing_from_marker_cannot_ground_this_fact(field):
    payload, model_input, snapshot = semantic_fixture()
    payload["facts"][0][field] = {"surface": "강연오", "document_id": None}
    payload["facts"][0]["statement"] += " 강연오도 있었다."
    assert_failed(checked(payload, model_input, snapshot), "INVALID_FACT")


@pytest.mark.parametrize("field", ["actor", "recipient", "object"])
def test_v4_role_surface_elsewhere_in_chunk_cannot_ground_short_excerpt(field):
    payload, model_input, snapshot = semantic_fixture(
        schema_version="refresh-candidate-v4"
    )
    payload["facts"][0][field] = {"surface": "윤준", "document_id": None}
    payload["facts"][0]["statement"] += " 윤준도 있었다."
    assert_failed(checked(payload, model_input, snapshot), "INVALID_FACT")


@pytest.mark.parametrize("field", ["actor", "recipient"])
def test_named_role_cannot_point_to_another_loaded_identity(field):
    payload, model_input, snapshot = semantic_fixture()
    payload["facts"][0][field]["document_id"] = "yunjun"
    assert_failed(checked(payload, model_input, snapshot), "INVALID_FACT")


def test_named_roles_can_use_explicit_content_aliases():
    excerpt = "해주는 도윤에게 방문 전달을 맡겼다."
    payload, model_input, snapshot = semantic_fixture(excerpt)
    fact = payload["facts"][0]
    fact["actor"]["surface"] = "해주"
    fact["recipient"]["surface"] = "도윤"
    fact["statement"] = excerpt
    result = checked(payload, model_input, snapshot)
    assert result.outcome == Outcome.PROPOSED
    assert result.facts[0].actor == FactEntity("해주", "haeju")
    assert result.facts[0].recipient == FactEntity("도윤", "doyun")


@pytest.mark.parametrize(
    "role,document_id", [("actor", "haeju"), ("recipient", "yunjun")]
)
def test_anonymous_official_and_mother_cannot_be_assigned_to_loaded_characters(
    role, document_id
):
    payload, model_input, snapshot = semantic_fixture(ANONYMOUS_EXCERPT)
    fact = payload["facts"][0]
    fact.update(
        actor={"surface": "반환 담당자", "document_id": None},
        recipient={"surface": "어머니", "document_id": None},
        object={"surface": "남은 기억 일부", "document_id": None},
        action="돌려주었다",
        extent="PARTIAL",
        statement=ANONYMOUS_EXCERPT,
    )
    payload["relation_proposals"] = []
    payload["document_proposals"] = [
        {
            "target_document_id": "yunjun",
            "base_revision_no": 1,
            "field": "body_text",
            "value": "윤준은 살아 있다. 반환 담당자는 어머니에게 남은 기억 일부를 돌려주었다.",
            "evidence": [{"marker": fact["source"]["marker"]}],
            "fact_ids": ["F0001"],
        }
    ]
    candidate = parse(payload, model_input)
    assert candidate.facts[0].recipient == FactEntity("어머니", None)
    assert (
        RefreshRules().validate_candidate(snapshot, candidate, model_input).outcome
        == Outcome.PROPOSED
    )
    fact[role]["document_id"] = document_id
    assert_failed(checked(payload, model_input, snapshot), "INVALID_FACT")


@pytest.mark.parametrize("field", ["actor", "recipient", "object"])
def test_statement_must_preserve_every_annotated_role_surface(field):
    payload, model_input, snapshot = semantic_fixture()
    fact = payload["facts"][0]
    fact["statement"] = fact["statement"].replace(fact[field]["surface"], "그 사람")
    assert_failed(checked(payload, model_input, snapshot), "INVALID_FACT")


@pytest.mark.parametrize("field", ["certainty", "extent", "time"])
def test_invalid_fact_classification_is_rejected_at_strict_schema(field):
    payload, model_input, _ = semantic_fixture()
    payload["facts"][0][field] = "invented"
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("field,value", [("action", "승인했다"), ("statement", "")])
def test_action_and_statement_are_bounded_by_fact_contract(field, value):
    payload, model_input, snapshot = semantic_fixture()
    payload["facts"][0][field] = value
    assert_failed(checked(payload, model_input, snapshot), "INVALID_FACT")


@pytest.mark.parametrize("fact_ids", [[], ["unknown"]])
def test_current_relation_requires_known_nonempty_fact_references(fact_ids):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    relation = replace(candidate.relation_proposals[0], fact_ids=tuple(fact_ids))
    assert_failed(
        RefreshRules().validate_candidate(
            snapshot, replace(candidate, relation_proposals=(relation,)), model_input
        ),
        "INVALID_FACT_REFERENCE",
    )


@pytest.mark.parametrize("kind", ["document", "new_document"])
@pytest.mark.parametrize("fact_ids", [[], ["unknown"]])
def test_updates_and_new_settings_also_require_fact_references(kind, fact_ids):
    payload, model_input, snapshot = semantic_fixture()
    marker = payload["facts"][0]["source"]["marker"]
    if kind == "document":
        payload["document_proposals"] = [
            {
                "target_document_id": "haeju",
                "base_revision_no": 3,
                "field": "body_text",
                "value": snapshot.documents[1].body_text + " " + NAMED_EXCERPT,
                "evidence": [{"marker": marker}],
                "fact_ids": ["F0001"],
            }
        ]
    else:
        payload["new_document_proposals"] = [
            {
                "candidate_id": "new:EVENT:방문 전달 배정",
                "folder_code": "EVENT",
                "name": "방문 전달 배정",
                "body_text": NAMED_EXCERPT,
                "evidence": [{"marker": marker}],
                "fact_ids": ["F0001"],
            }
        ]
    candidate = parse(payload, model_input)
    assert (
        RefreshRules().validate_candidate(snapshot, candidate, model_input).outcome
        == Outcome.PROPOSED
    )
    if kind == "document":
        candidate = replace(
            candidate,
            document_proposals=(
                replace(candidate.document_proposals[0], fact_ids=tuple(fact_ids)),
            ),
        )
    else:
        candidate = replace(
            candidate,
            new_document_proposals=(
                replace(candidate.new_document_proposals[0], fact_ids=tuple(fact_ids)),
            ),
        )
    assert_failed(
        RefreshRules().validate_candidate(snapshot, candidate, model_input),
        "INVALID_FACT_REFERENCE",
    )


def test_current_wire_relation_does_not_accept_free_description():
    payload, model_input, _ = semantic_fixture()
    payload["relation_proposals"][0]["description"] = "민해주는 조사를 승인했다."
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("defect", ["description", "evidence"])
def test_relation_cannot_add_claims_or_evidence_outside_referenced_facts(defect):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    relation = candidate.relation_proposals[0]
    if defect == "description":
        relation = replace(relation, description="민해주는 조사를 승인했다.")
    else:
        source = snapshot.documents[0]
        quote = "윤준은 어머니를 기다렸다."
        start = source.body_text.index(quote)
        relation = replace(
            relation,
            evidence=(Evidence("chapter", 2, start, start + len(quote), quote),),
        )
    assert_failed(
        RefreshRules().validate_candidate(
            snapshot, replace(candidate, relation_proposals=(relation,)), model_input
        ),
        "INVALID_RELATION_FACT",
    )


def test_fact_ids_are_content_based_across_calls_and_local_wire_names():
    payload, model_input, snapshot = semantic_fixture()
    first = parse(payload, model_input)
    renamed = deepcopy(payload)
    renamed["facts"][0]["fact_id"] = "F9999"
    renamed["relation_proposals"][0]["fact_ids"] = ["F9999"]
    second = parse(renamed, model_input)
    assert first.facts == second.facts
    assert first.relation_proposals == second.relation_proposals
    aggregate = replace(
        first,
        facts=tuple(
            {fact.fact_id: fact for fact in first.facts + second.facts}.values()
        ),
        relation_proposals=first.relation_proposals + second.relation_proposals,
    )
    result = RefreshRules().validate_candidate(snapshot, aggregate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert len(result.facts) == len(result.relation_proposals) == 1


def test_same_local_wire_id_for_different_facts_does_not_collide():
    payload, model_input, _ = semantic_fixture()
    first = parse(payload, model_input)
    payload["facts"][0]["statement"] = "민해주는 백도윤에게 방문 전달 업무를 맡겼다."
    second = parse(payload, model_input)
    assert first.facts[0].fact_id != second.facts[0].fact_id
    assert first.relation_proposals[0].fact_ids != second.relation_proposals[0].fact_ids


def test_identical_facts_with_distinct_wire_ids_normalize_references_once():
    payload, model_input, snapshot = semantic_fixture()
    repeated = deepcopy(payload["facts"][0])
    repeated["fact_id"] = "F0002"
    payload["facts"].append(repeated)
    payload["relation_proposals"][0]["fact_ids"] = ["F0001", "F0002"]
    candidate = parse(payload, model_input)
    assert len(candidate.facts) == 1
    assert candidate.relation_proposals[0].fact_ids == (candidate.facts[0].fact_id,)
    assert candidate.relation_proposals[0].description == NAMED_EXCERPT
    assert (
        RefreshRules().validate_candidate(snapshot, candidate, model_input).outcome
        == Outcome.PROPOSED
    )


def test_duplicate_raw_fact_reference_is_rejected_before_normalization():
    payload, model_input, _ = semantic_fixture()
    payload["relation_proposals"][0]["fact_ids"] = ["F0001", "F0001"]
    with pytest.raises(RefreshFailure) as caught:
        parse(payload, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"


def test_direct_candidate_with_duplicate_fact_id_is_rejected():
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    assert_failed(
        RefreshRules().validate_candidate(
            snapshot, replace(candidate, facts=candidate.facts * 2), model_input
        ),
        "INVALID_FACT",
    )


@pytest.mark.parametrize("defect", ["excerpt", "nonchunk", "overbudget"])
def test_v5_direct_fact_cannot_use_arbitrary_span_or_exceed_chunk_budget(defect):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    source = snapshot.documents[0]
    if defect == "overbudget":
        source = replace(
            source, body_text=source.body_text + "\n" + "추가 원문. " * 200
        )
        snapshot = replace(snapshot, documents=(source, *snapshot.documents[1:]))
        start, end = 0, len(source.body_text)
        assert end > snapshot.request.settings.chunk_chars
    elif defect == "excerpt":
        start = source.body_text.index(NAMED_EXCERPT)
        end = start + len(NAMED_EXCERPT)
    else:
        start, end = 0, source.body_text.index("\n기록 뒷부분.")
    evidence = Evidence("chapter", 2, start, end, source.body_text[start:end])
    fact = replace(candidate.facts[0], evidence=(evidence,))
    relation = replace(candidate.relation_proposals[0], evidence=(evidence,))
    assert_failed(
        RefreshRules().validate_candidate(
            snapshot,
            replace(candidate, facts=(fact,), relation_proposals=(relation,)),
            model_input,
        ),
        "INVALID_FACT",
    )


def test_service_aggregates_shared_fact_across_separate_update_calls_once():
    payload, model_input, snapshot = semantic_fixture()
    request = replace(
        snapshot.request,
        settings=replace(snapshot.request.settings, separate_discovery=True),
    )
    snapshot = replace(snapshot, request=request)
    marker = payload["facts"][0]["source"]["marker"]
    unused_payload = deepcopy(payload)
    unused_payload["facts"] = [unused_fact_payload(payload)]
    unused_payload["relation_proposals"] = []
    unused = parse(unused_payload, model_input)
    by_target = {}
    for target, revision, body in (
        ("haeju", 3, snapshot.documents[1].body_text),
        ("doyun", 4, snapshot.documents[2].body_text),
    ):
        current = deepcopy(payload)
        current["document_proposals"] = [
            {
                "target_document_id": target,
                "base_revision_no": revision,
                "field": "body_text",
                "value": body + " " + NAMED_EXCERPT,
                "evidence": [{"marker": marker}],
                "fact_ids": ["F0001"],
            }
        ]
        if target == "doyun":
            current["relation_proposals"] = []
        current["facts"].append(unused_fact_payload(payload))
        by_target[(target,)] = parse(current, model_input)

    seen = []
    aggregates = []

    class FixedModel:
        def generate(self, current_input):
            seen.append(current_input.target_ids)
            return by_target.get(current_input.target_ids, unused)

    class RecordingRules(RefreshRules):
        def validate_candidate(self, current_snapshot, candidate, current_input):
            if current_input.relation_scope is None:
                aggregates.append(candidate)
            return super().validate_candidate(
                current_snapshot, candidate, current_input
            )

    service = RefreshService(
        artifacts=SimpleNamespace(
            read_context=lambda current_request: snapshot,
            save_context=lambda current_snapshot: ArtifactRef("bucket", "context"),
        ),
        relations=object(),
        documents=object(),
        model=FixedModel(),
        jobs=SimpleNamespace(checkpoint=lambda record: None),
        publisher=object(),
        rules=RecordingRules(),
    )
    result = service._compute(request, JobRecord(request, "execution"))
    assert seen == [(), ("haeju",), ("doyun",), ("yunjun",)]
    assert result.outcome == Outcome.PROPOSED
    assert len(result.facts) == 1
    assert len(result.document_proposals) == 2
    assert len(result.relation_proposals) == 1
    assert result.usage.calls == 4
    assert len(aggregates) == 1
    assert aggregates[0].facts == result.facts
    assert unused.facts[0].fact_id not in {fact.fact_id for fact in result.facts}


def test_aggregate_fact_limit_is_separate_from_one_call_limit():
    payload, model_input, snapshot = semantic_fixture()
    template = parse(payload, model_input).facts[0]
    quotes = [
        f"민해주는 백도윤에게 방문 전달 {index:03}을 맡겼다." for index in range(101)
    ]
    text = "\n".join(quotes)
    source = replace(snapshot.documents[0], body_text=text)
    request = replace(
        snapshot.request,
        settings=replace(snapshot.request.settings, chunk_chars=len(text)),
    )
    snapshot = replace(
        snapshot, request=request, documents=(source, *snapshot.documents[1:])
    )
    facts = []
    for index, quote in enumerate(quotes):
        facts.append(
            replace(
                template,
                fact_id=f"F{index:04}",
                object=FactEntity(f"방문 전달 {index:03}", None),
                statement=quote,
                evidence=(Evidence("chapter", 2, 0, len(text), text),),
            )
        )
    candidate = ModelCandidate(
        (
            DocumentProposal(
                "haeju",
                3,
                "body_text",
                snapshot.documents[1].body_text + " " + quotes[0],
                (Evidence("chapter", 2, 0, len(text), text),),
                tuple(fact.fact_id for fact in facts),
            ),
        ),
        facts=tuple(facts),
    )
    per_call = replace(
        model_input,
        relation_scope=RelationGenerationScope(model_input.target_ids, True),
    )
    assert_failed(
        RefreshRules().validate_candidate(snapshot, candidate, per_call), "INVALID_FACT"
    )
    aggregate = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert aggregate.outcome == Outcome.PROPOSED
    assert len(aggregate.facts) == 101
    request = replace(
        snapshot.request, settings=replace(snapshot.request.settings, max_model_calls=1)
    )
    assert_failed(
        RefreshRules().validate_candidate(
            replace(snapshot, request=request), candidate, model_input
        ),
        "INVALID_FACT",
    )


def test_valid_unused_fact_gives_no_change_and_is_pruned_from_result():
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    result = RefreshRules().validate_candidate(
        snapshot, replace(candidate, relation_proposals=()), model_input
    )
    assert result.outcome == Outcome.NO_CHANGE
    assert result.failure is None
    assert not result.document_proposals
    assert not result.relation_proposals
    assert not result.new_document_proposals
    assert not result.facts


def test_valid_unused_fact_does_not_fail_supported_proposal_or_escape_into_result():
    payload, model_input, snapshot = semantic_fixture()
    payload["facts"].append(unused_fact_payload(payload))
    candidate = parse(payload, model_input)
    assert len(candidate.facts) == 2
    result = RefreshRules().validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert len(result.facts) == 1
    assert result.relation_proposals[0].fact_ids == (result.facts[0].fact_id,)
    assert result.facts[0].statement == NAMED_EXCERPT


@pytest.mark.parametrize(
    "defect,code",
    [
        ("source", "INVALID_EVIDENCE"),
        ("identity", "INVALID_FACT"),
        ("type", "INVALID_FACT"),
    ],
)
def test_invalid_unused_fact_still_fails_even_without_any_final_proposal(defect, code):
    payload, model_input, snapshot = semantic_fixture()
    candidate = parse(payload, model_input)
    fact = candidate.facts[0]
    if defect == "source":
        fact = replace(fact, evidence=(replace(fact.evidence[0], quote="변조된 원문"),))
    elif defect == "identity":
        fact = replace(fact, actor=FactEntity("민해주", "yunjun"))
    else:
        fact = {"fact_id": fact.fact_id}
    assert_failed(
        RefreshRules().validate_candidate(
            snapshot,
            replace(candidate, relation_proposals=(), facts=(fact,)),
            model_input,
        ),
        code,
    )


@pytest.mark.parametrize("version", ["refresh-candidate-v1", "refresh-candidate-v2"])
def test_explicit_legacy_candidate_can_be_read_without_new_fact_annotations(version):
    payload, model_input, snapshot = semantic_fixture()
    current = parse(payload, model_input)
    raw = asdict(current.relation_proposals[0])
    raw.pop("fact_ids")
    legacy = {"document_proposals": [], "relation_proposals": [raw]}
    if version == "refresh-candidate-v2":
        legacy["new_document_proposals"] = []
    candidate = parse_candidate(json.dumps(legacy), Usage(1), schema_version=version)
    assert candidate.relation_proposals[0].description == NAMED_EXCERPT
    result = RefreshRules().validate_candidate(
        snapshot, candidate, replace(model_input, schema_version=version)
    )
    assert result.outcome == Outcome.PROPOSED
    assert not result.facts
    assert_failed(
        RefreshRules().validate_candidate(snapshot, candidate, model_input),
        "INVALID_FACT_REFERENCE",
    )


def test_explicit_v3_marker_candidate_remains_readable_without_fact_annotations():
    payload, model_input, snapshot = semantic_fixture()
    legacy = deepcopy(payload)
    legacy.pop("facts")
    relation = legacy["relation_proposals"][0]
    relation.pop("fact_ids")
    relation["description"] = NAMED_EXCERPT
    relation["evidence"] = [{"marker": payload["facts"][0]["source"]["marker"]}]
    candidate = parse_candidate(
        json.dumps(legacy),
        Usage(1),
        schema_version="refresh-candidate-v3",
        model_input=model_input,
    )
    assert candidate.relation_proposals[0].description == NAMED_EXCERPT
    assert (
        candidate.relation_proposals[0].evidence[0].quote
        == snapshot.documents[0].body_text
    )
    result = RefreshRules().validate_candidate(
        snapshot,
        candidate,
        replace(model_input, schema_version="refresh-candidate-v3"),
    )
    assert result.outcome == Outcome.PROPOSED
    with pytest.raises(RefreshFailure) as caught:
        parse(legacy, model_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"
