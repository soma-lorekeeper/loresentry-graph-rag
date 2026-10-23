"""Two bounded repairs preserve scope, atomic results, and all attempt usage."""

import json
from collections import deque
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from app.adapters.llm_schema import parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    FactEntity,
    Failure,
    InputSnapshot,
    ModelCandidate,
    Outcome,
    Usage,
    ValidationDiagnostic,
)
from app.refresh.repair import build_repair_input
from app.refresh.rules import RefreshRules
from app.refresh.service import RefreshService
from tests.fakes.refresh import (
    Calls,
    FakeDocuments,
    FakeInbox,
    FakeJsonArtifacts,
    FakePublisher,
    FakeRelations,
)
from tests.refresh.test_semantic_relations import (
    NAMED_EXCERPT,
    parse,
    semantic_fixture,
    unused_fact_payload,
)


class ScriptedModel:
    def __init__(self):
        self.inputs = []
        self.responses = deque()

    def generate(self, model_input):
        self.inputs.append(model_input)
        assert self.responses, "Unexpected model call"
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(model_input)
        return response


def repair_scenario(*, target_ids=("haeju",), max_model_calls=20, rules=None):
    payload, model_input, snapshot = semantic_fixture()
    request = replace(
        snapshot.request,
        target_ids=target_ids,
        settings=replace(snapshot.request.settings, max_model_calls=max_model_calls),
    )
    snapshot = replace(snapshot, request=request, target_ids=target_ids)
    source = InputSnapshot(request.job, (snapshot.documents[0],))
    calls = Calls()
    scenario = SimpleNamespace(
        request=request,
        snapshot=snapshot,
        payload=payload,
        model_input=model_input,
        calls=calls,
        artifacts=FakeJsonArtifacts({request.input_ref: source}, calls),
        documents=FakeDocuments(snapshot.documents[1:], calls),
        relations=FakeRelations(snapshot.related, calls),
        inbox=FakeInbox(calls=calls),
        publisher=FakePublisher(calls),
        model=ScriptedModel(),
    )
    scenario.service = RefreshService(
        **{
            name: getattr(scenario, name)
            for name in (
                "artifacts",
                "documents",
                "relations",
                "inbox",
                "model",
            )
        },
        rules=rules,
    )
    return scenario


def candidate_for(scenario, target="haeju", *, value=None, usage=None):
    usage = Usage(1, 20, 30) if usage is None else usage
    payload = json.loads(json.dumps(scenario.payload))
    document = next(
        document
        for document in scenario.snapshot.documents
        if document.document_id == target
    )
    payload["relation_proposals"] = []
    payload["document_proposals"] = [
        {
            "target_document_id": target,
            "base_revision_no": document.revision_no,
            "field": "body_text",
            "value": value or document.body_text + " " + NAMED_EXCERPT,
            "evidence": [{"marker": payload["facts"][0]["source"]["marker"]}],
            "fact_ids": ["F0001"],
        }
    ]
    return replace(parse(payload, scenario.model_input), usage=usage)


def invalid_identity(candidate):
    fact = replace(candidate.facts[0], actor=FactEntity("민해주", "unknown-character"))
    return replace(candidate, facts=(fact,))


def parser_failure(*, usage=None, candidate_text='{"facts":[]}'):
    usage = Usage(1, 11, 13) if usage is None else usage
    diagnostic = ValidationDiagnostic(
        "facts[0].source.marker",
        "known_source_marker",
        fact_id="F1",
        source_marker="unknown",
        offending_value="unknown",
    )
    return RefreshFailure(
        Failure("MODEL_INVALID_RESPONSE", "Unknown source marker"),
        diagnostics=(diagnostic,),
        usage=usage,
        candidate_text=candidate_text,
    )


def run_saved(scenario):
    scenario.artifacts.save_context(scenario.snapshot)
    completion = scenario.service.run(scenario.request)
    return completion, scenario.artifacts.read_result(scenario.request)


def assert_atomic_failure(result, code):
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == code
    assert not result.document_proposals
    assert not result.relation_proposals
    assert not result.new_document_proposals
    assert not result.facts


def complete_candidate_for_repair(scenario):
    payload = json.loads(json.dumps(scenario.payload))
    waiting = unused_fact_payload(payload)
    waiting["fact_id"] = "F0002"
    payload["facts"].append(waiting)
    marker = payload["facts"][0]["source"]["marker"]
    payload["document_proposals"] = [
        {
            "target_document_id": "haeju",
            "base_revision_no": 3,
            "field": "body_text",
            "value": NAMED_EXCERPT + " 윤준은 어머니를 기다렸다.",
            "evidence": [{"marker": marker}],
            "fact_ids": ["F0001", "F0002"],
        }
    ]
    payload["new_document_proposals"] = [
        {
            "candidate_id": "new:EVENT:방문 전달",
            "folder_code": "EVENT",
            "name": "방문 전달",
            "body_text": NAMED_EXCERPT,
            "evidence": [{"marker": marker}],
            "fact_ids": ["F0001"],
        }
    ]
    return parse(payload, scenario.model_input)


def test_typed_repair_short_ids_preserve_all_proposal_refs_and_canonical_roundtrip():
    scenario = repair_scenario()
    candidate = complete_candidate_for_repair(scenario)
    original_ids = [fact.fact_id for fact in candidate.facts]
    assert all(len(fact_id) == 65 for fact_id in original_ids)
    marker = scenario.payload["facts"][0]["source"]["marker"]
    diagnostics = tuple(
        ValidationDiagnostic(
            f"facts[{index}].action",
            "fact.action_in_source",
            fact_id=fact.fact_id,
            source_marker=marker,
            offending_value="오류 표현",
        )
        for index, fact in enumerate(candidate.facts)
    )
    repaired_input = build_repair_input(scenario.model_input, candidate, diagnostics)
    context = json.loads(repaired_input.prompt)
    feedback = context.pop("validation_repair")
    assert context == json.loads(scenario.model_input.prompt)
    previous = feedback["previous_candidate"]
    short_ids = [fact["fact_id"] for fact in previous["facts"]]
    assert short_ids == ["F0001", "F0002"]
    aliases = dict(zip(original_ids, short_ids, strict=True))
    assert [item["fact_id"] for item in feedback["diagnostics"]] == short_ids
    assert not any(fact_id in json.dumps(feedback) for fact_id in original_ids)
    for field in (
        "document_proposals",
        "relation_proposals",
        "new_document_proposals",
    ):
        for original, wire in zip(
            getattr(candidate, field), previous[field], strict=True
        ):
            assert wire["fact_ids"] == [aliases[key] for key in original.fact_ids]
            assert set(wire["fact_ids"]).issubset(short_ids)
    reparsed = parse(previous, repaired_input)
    assert reparsed == candidate
    assert [fact.fact_id for fact in reparsed.facts] == original_ids


@pytest.mark.parametrize("unknown_ref", ["undeclared-fact", "F0001"])
def test_repair_preserves_unknown_refs_without_alias_collision(unknown_ref):
    scenario = repair_scenario()
    original = complete_candidate_for_repair(scenario)
    document = replace(
        original.document_proposals[0],
        fact_ids=(*original.document_proposals[0].fact_ids, unknown_ref),
    )
    candidate = replace(original, document_proposals=(document,))
    diagnostic = ValidationDiagnostic(
        "document_proposals[0].fact_ids[2]",
        "fact.reference_known",
        fact_id=unknown_ref,
        offending_value=unknown_ref,
    )
    repaired_input = build_repair_input(scenario.model_input, candidate, (diagnostic,))
    feedback = json.loads(repaired_input.prompt)["validation_repair"]
    previous = feedback["previous_candidate"]
    declarations = {fact["fact_id"] for fact in previous["facts"]}
    assert unknown_ref not in declarations
    assert previous["document_proposals"][0]["fact_ids"][-1] == unknown_ref
    assert feedback["diagnostics"] == [asdict(diagnostic)]
    assert set(previous["document_proposals"][0]["fact_ids"][:-1]) == declarations
    if unknown_ref == "F0001":
        assert declarations == {"F0002", "F0003"}
    with pytest.raises(RefreshFailure) as caught:
        parse(previous, repaired_input)
    assert caught.value.failure.code == "MODEL_INVALID_RESPONSE"
    assert caught.value.diagnostics[0].rule == "fact.reference_known"
    assert caught.value.diagnostics[0].offending_value == unknown_ref


@pytest.mark.parametrize(
    "rule,remapped",
    [
        ("fact.identifier_unique", True),
        ("fact.reference_unique", True),
        ("fact.reference_known", True),
        ("fact.reference_source_coverage", True),
        ("fact.action_in_source", False),
    ],
)
def test_repair_only_rewrites_identifier_diagnostic_values(rule, remapped):
    scenario = repair_scenario()
    candidate = candidate_for(scenario)
    canonical_id = candidate.facts[0].fact_id
    diagnostic = ValidationDiagnostic(
        "facts[0].action",
        rule,
        fact_id=canonical_id,
        offending_value=canonical_id,
    )
    repaired_input = build_repair_input(scenario.model_input, candidate, (diagnostic,))
    feedback = json.loads(repaired_input.prompt)["validation_repair"]
    item = feedback["diagnostics"][0]
    assert item["fact_id"] == "F0001"
    assert item["offending_value"] == ("F0001" if remapped else canonical_id)
    assert item["path"] == diagnostic.path


@pytest.mark.parametrize("candidate_text", ['{"facts":[]}', '{"facts":'])
def test_parser_repair_preserves_context_scope_and_counts_failed_response_usage(
    candidate_text,
):
    scenario = repair_scenario()
    error = parser_failure(candidate_text=candidate_text)
    repaired = candidate_for(scenario, usage=Usage(1, 17, 19))
    scenario.model.responses.extend((error, repaired))
    completion, result = run_saved(scenario)
    assert completion.outcome == Outcome.PROPOSED
    assert result.document_proposals == repaired.document_proposals
    assert result.usage == Usage(2, 28, 32)
    initial_input, repair_input = scenario.model.inputs
    initial_context = json.loads(initial_input.prompt)
    repaired_context = json.loads(repair_input.prompt)
    feedback = repaired_context.pop("validation_repair")
    assert repaired_context == initial_context
    assert feedback == {
        "version": "refresh-validation-repair-v2",
        "attempt": 1,
        "previous_candidate": (
            json.loads(candidate_text)
            if candidate_text.endswith("}")
            else candidate_text
        ),
        "diagnostics": [asdict(error.diagnostics[0])],
        "protected_proposal_keys": {},
    }
    for field in (
        "target_ids",
        "relation_scope",
        "allow_document_updates",
        "allow_new_documents",
        "schema_version",
        "prompt_version",
        "model",
    ):
        assert getattr(repair_input, field) == getattr(initial_input, field)
    assert repair_input.instructions.startswith(initial_input.instructions)
    assert repair_input.instructions != initial_input.instructions
    assert [(a.call_index, a.attempt_index) for a in result.attempts] == [
        (1, 0),
        (1, 1),
    ]
    assert [a.outcome for a in result.attempts] == [Outcome.FAILED, Outcome.PROPOSED]
    assert [a.usage for a in result.attempts] == [error.usage, repaired.usage]
    assert result.attempts[0].document_proposals is None
    assert result.attempts[0].diagnostics == error.diagnostics
    assert result.attempts[1].document_proposals == 1
    assert result.attempts[1].relation_proposals == 0
    assert result.attempts[1].new_document_proposals == 0


def test_runtime_validation_repair_replaces_entire_failed_candidate():
    scenario = repair_scenario()
    original = candidate_for(scenario, value="원후보의 내용만 남기면 안 된다.")
    payload = json.loads(json.dumps(scenario.payload))
    payload["document_proposals"] = []
    payload["relation_proposals"] = []
    payload["new_document_proposals"] = [
        {
            "candidate_id": "new:EVENT:버려야 할 원후보",
            "folder_code": "EVENT",
            "name": "버려야 할 원후보",
            "body_text": NAMED_EXCERPT,
            "evidence": [{"marker": payload["facts"][0]["source"]["marker"]}],
            "fact_ids": ["F0001"],
        }
    ]
    original = replace(
        original,
        new_document_proposals=parse(
            payload, scenario.model_input
        ).new_document_proposals,
    )
    original = invalid_identity(original)
    repaired = candidate_for(scenario, usage=Usage(1, 5, 7))
    scenario.model.responses.extend((original, repaired))
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert result.document_proposals == repaired.document_proposals
    assert not result.new_document_proposals
    assert result.facts == repaired.facts
    assert result.usage == Usage(2, 25, 37)
    assert result.attempts[0].new_document_proposals == 1
    assert result.attempts[1].new_document_proposals == 0
    previous = json.loads(scenario.model.inputs[1].prompt)["validation_repair"][
        "previous_candidate"
    ]
    assert previous["new_document_proposals"][0]["candidate_id"] == (
        "new:EVENT:버려야 할 원후보"
    )
    assert previous["facts"][0]["source"] == {
        "marker": scenario.payload["facts"][0]["source"]["marker"]
    }
    assert previous["document_proposals"][0]["fact_ids"] == [
        previous["facts"][0]["fact_id"]
    ]


def test_actual_marker_parser_failure_carries_usage_and_diagnostics_into_repair():
    scenario = repair_scenario()
    raw = json.loads(json.dumps(scenario.payload))
    raw["facts"][0]["source"]["marker"] = "unknown"
    text = json.dumps(raw, ensure_ascii=False)

    def invalid_response(model_input):
        return parse_candidate(
            text,
            Usage(1, 11, 13),
            schema_version="refresh-candidate-v5",
            model_input=model_input,
        )

    scenario.model.responses.extend(
        (invalid_response, candidate_for(scenario, usage=Usage(1, 17, 19)))
    )
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert result.usage == Usage(2, 28, 32)
    failed = result.attempts[0]
    assert failed.failure.code == "MODEL_INVALID_RESPONSE"
    assert failed.diagnostics[0].fact_id == "F0001"
    assert failed.diagnostics[0].source_marker == "unknown"
    repair = json.loads(scenario.model.inputs[1].prompt)["validation_repair"]
    assert repair["previous_candidate"] == raw


def test_third_validation_failure_stops_atomically_after_two_repairs():
    scenario = repair_scenario()
    first = invalid_identity(candidate_for(scenario, usage=Usage(1, 11, 13)))
    second = invalid_identity(candidate_for(scenario, usage=Usage(1, 17, 19)))
    third = invalid_identity(candidate_for(scenario, usage=Usage(1, 23, 29)))
    scenario.model.responses.extend((first, second, third, candidate_for(scenario)))
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "INVALID_FACT")
    assert len(scenario.model.inputs) == 3
    assert len(scenario.model.responses) == 1
    assert result.usage == Usage(3, 51, 61)
    assert [a.outcome for a in result.attempts] == [Outcome.FAILED] * 3
    assert [a.attempt_index for a in result.attempts] == [0, 1, 2]
    assert (
        json.loads(scenario.model.inputs[2].prompt)["validation_repair"]["attempt"] == 2
    )
    assert all(a.diagnostics for a in result.attempts)


@pytest.mark.parametrize("repair_all", [True, False])
def test_all_fact_leaf_diagnostics_are_sent_in_bounded_repairs(repair_all):
    scenario = repair_scenario()
    valid = candidate_for(scenario)
    waiting_payload = json.loads(json.dumps(scenario.payload))
    waiting_payload["facts"] = [unused_fact_payload(scenario.payload)]
    waiting_payload["relation_proposals"] = []
    waiting = parse(waiting_payload, scenario.model_input).facts[0]
    document = replace(
        valid.document_proposals[0],
        value=valid.document_proposals[0].value + " 윤준은 어머니를 기다렸다.",
        fact_ids=(valid.facts[0].fact_id, waiting.fact_id),
    )
    valid = replace(
        valid, facts=(*valid.facts, waiting), document_proposals=(document,)
    )
    missing_object = replace(waiting, object=FactEntity("알 수 없는 물건", None))
    wrong_action_and_identity = replace(
        valid.facts[0],
        action="넘겼다",
        recipient=FactEntity("백도윤", "haeju"),
    )
    invalid = replace(valid, facts=(wrong_action_and_identity, missing_object))
    repaired = (
        valid if repair_all else replace(valid, facts=(valid.facts[0], missing_object))
    )
    scenario.model.responses.extend((invalid, repaired, valid))
    _, result = run_saved(scenario)
    count = 2 if repair_all else 3
    assert len(scenario.model.inputs) == count
    assert len(scenario.model.responses) == 3 - count
    assert result.usage == Usage(count, count * 20, count * 30)
    first = result.attempts[0]
    assert first.outcome == Outcome.FAILED
    assert {(d.path, d.rule) for d in first.diagnostics} == {
        ("facts[0].action", "fact.action_in_source"),
        ("facts[1].object.surface", "fact.role_surface_in_source"),
    }
    assert {d.fact_id for d in first.diagnostics} == {
        valid.facts[0].fact_id,
        waiting.fact_id,
    }
    marker = scenario.payload["facts"][0]["source"]["marker"]
    assert all(d.source_marker == marker for d in first.diagnostics)
    feedback = json.loads(scenario.model.inputs[1].prompt)["validation_repair"]
    aliases = {
        fact.fact_id: f"F{index:04d}"
        for index, fact in enumerate(invalid.facts, start=1)
    }
    assert feedback["diagnostics"] == [
        asdict(replace(diagnostic, fact_id=aliases[diagnostic.fact_id]))
        for diagnostic in first.diagnostics
    ]
    assert len(feedback["previous_candidate"]["facts"]) == 2
    assert result.outcome == Outcome.PROPOSED
    assert result.document_proposals == valid.document_proposals
    assert set(result.facts) == set(valid.facts)
    if repair_all:
        assert result.attempts[1].outcome == Outcome.PROPOSED
    else:
        assert result.attempts[1].outcome == Outcome.FAILED
        assert result.attempts[2].outcome == Outcome.PROPOSED
        assert [(d.path, d.rule) for d in result.attempts[1].diagnostics] == [
            ("facts[1].object.surface", "fact.role_surface_in_source")
        ]
        latest = json.loads(scenario.model.inputs[2].prompt)["validation_repair"]
        assert latest["attempt"] == 2
        assert len(latest["diagnostics"]) == 1


@pytest.mark.parametrize("defect", ["source", "status"])
def test_fact_source_and_status_damage_still_stop_before_leaf_collection(defect):
    scenario = repair_scenario()
    candidate = invalid_identity(candidate_for(scenario))
    fact = replace(candidate.facts[0], action="넘겼다")
    if defect == "source":
        fact = replace(fact, evidence=(replace(fact.evidence[0], quote="잘못된 원문"),))
        code, path, rule = (
            "INVALID_EVIDENCE",
            "facts[0].source",
            "source.original_matches",
        )
    else:
        fact = replace(fact, certainty="invented")
        code, path, rule = "INVALID_FACT", "facts[0].certainty", "fact.certainty_known"
    model_input = RefreshRules().model_inputs(
        scenario.snapshot, RefreshRules().chunk(scenario.snapshot)
    )[0]
    result = RefreshRules().validate_candidate(
        scenario.snapshot, replace(candidate, facts=(fact,)), model_input
    )
    assert_atomic_failure(result, code)
    assert [(d.path, d.rule) for d in result.diagnostics] == [(path, rule)]


def test_repairing_later_call_reuses_earlier_successful_candidate():
    scenario = repair_scenario(target_ids=("haeju", "doyun"))
    earlier = candidate_for(scenario, usage=Usage(1, 5, 7))
    later = candidate_for(scenario, "doyun", usage=Usage(1, 17, 19))
    broken = invalid_identity(replace(later, usage=Usage(1, 11, 13)))
    scenario.model.responses.extend((earlier, broken, later))
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.PROPOSED
    assert [item.target_ids for item in scenario.model.inputs] == [
        ("haeju",),
        ("doyun",),
        ("doyun",),
    ]
    assert set(result.document_proposals) == set(
        earlier.document_proposals + later.document_proposals
    )
    assert result.usage == Usage(3, 33, 39)
    assert [(a.call_index, a.attempt_index) for a in result.attempts] == [
        (1, 0),
        (2, 0),
        (2, 1),
    ]


@pytest.mark.parametrize("maximum,expected_calls", [(2, 1), (3, 3)])
def test_repair_budget_reserves_calls_for_remaining_targets(maximum, expected_calls):
    scenario = repair_scenario(target_ids=("haeju", "doyun"), max_model_calls=maximum)
    scenario.model.responses.extend(
        (
            parser_failure(),
            candidate_for(scenario),
            candidate_for(scenario, "doyun"),
        )
    )
    _, result = run_saved(scenario)
    assert len(scenario.model.inputs) == expected_calls
    assert result.usage.calls == expected_calls
    if maximum == 2:
        assert_atomic_failure(result, "CALL_BUDGET_EXCEEDED")
        assert len(result.attempts) == 1
    else:
        assert result.outcome == Outcome.PROPOSED
        assert {p.target_document_id for p in result.document_proposals} == {
            "haeju",
            "doyun",
        }


@pytest.mark.parametrize(
    "code",
    [
        "MODEL_AUTH_FAILED",
        "MODEL_QUOTA_EXHAUSTED",
        "MODEL_REQUEST_INVALID",
        "CALL_BUDGET_EXCEEDED",
        "CONTEXT_BUDGET_EXCEEDED",
    ],
)
def test_terminal_nonvalidation_failure_never_repairs_even_with_diagnostics(code):
    scenario = repair_scenario()
    diagnostic_error = parser_failure()
    error = RefreshFailure(
        Failure(code, "Cannot fix this with candidate repair"),
        diagnostics=diagnostic_error.diagnostics,
        usage=Usage(1, 11, 13),
        candidate_text=diagnostic_error.candidate_text,
    )
    scenario.model.responses.extend((error, candidate_for(scenario)))
    _, result = run_saved(scenario)
    assert_atomic_failure(result, code)
    assert len(scenario.model.inputs) == 1


def test_validation_failure_without_specific_diagnostic_does_not_retry():
    scenario = repair_scenario()
    error = RefreshFailure(
        Failure("MODEL_INVALID_RESPONSE", "Invalid candidate"), usage=Usage(1, 11, 13)
    )
    scenario.model.responses.extend((error, candidate_for(scenario)))
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "MODEL_INVALID_RESPONSE")
    assert len(scenario.model.inputs) == 1
    assert result.usage == Usage(1, 11, 13)


def test_explicit_legacy_scope_does_not_gain_current_validation_repair():
    scenario = repair_scenario()
    scenario.request = replace(
        scenario.request,
        model_settings=replace(
            scenario.request.model_settings, schema_version="refresh-candidate-v3"
        ),
    )
    scenario.snapshot = replace(scenario.snapshot, request=scenario.request)
    scenario.model.responses.extend(
        (invalid_identity(candidate_for(scenario)), candidate_for(scenario))
    )
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "INVALID_FACT")
    assert result.candidate_schema_version == "refresh-candidate-v3"
    assert len(scenario.model.inputs) == 1
    assert result.attempts[0].diagnostics


def test_transport_failure_is_left_to_delivery_without_internal_repair():
    scenario = repair_scenario()
    diagnostic_error = parser_failure()
    error = RefreshFailure(
        Failure("MODEL_TIMEOUT", "Timed out", True),
        diagnostics=diagnostic_error.diagnostics,
    )
    scenario.model.responses.extend((error, candidate_for(scenario)))
    scenario.artifacts.save_context(scenario.snapshot)
    with pytest.raises(RefreshFailure) as caught:
        scenario.service.run(scenario.request)
    assert caught.value.failure.code == "MODEL_TIMEOUT"
    assert len(scenario.model.inputs) == 1
    assert scenario.artifacts.read_result(scenario.request) is None
    assert not scenario.publisher.delivered


def test_preflight_evaluation_budget_rejection_does_not_count_an_attempt():
    scenario = repair_scenario()
    scenario.model.responses.append(
        RefreshFailure(Failure("EVALUATION_CALL_BUDGET", "No external call started"))
    )
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "EVALUATION_CALL_BUDGET")
    assert result.usage == Usage()
    assert not result.attempts


def test_repair_request_preflight_rejection_preserves_only_started_call_usage():
    scenario = repair_scenario()
    scenario.model.responses.extend(
        (
            parser_failure(),
            RefreshFailure(Failure("MODEL_REQUEST_INVALID", "Repair input too large")),
        )
    )
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "MODEL_REQUEST_INVALID")
    assert len(scenario.model.inputs) == 2
    assert result.usage == Usage(1, 11, 13)
    assert len(result.attempts) == 1


def test_repair_input_over_token_budget_stops_before_second_create():
    """보완 입력이 토큰 예산을 넘으면 어댑터의 계수에서 멈추고 두 번째 생성은 보내지 않는다."""
    from app.adapters.llm import ModelLimits, OpenAIProposalModel
    from tests.refresh.test_llm import client_fixture

    scenario = repair_scenario()
    # 실제 어댑터는 limits와 같은 모델 이름의 입력만 받는다.
    scenario.request = replace(
        scenario.request,
        model_settings=replace(scenario.request.model_settings, model="gpt-5.6-luna"),
    )
    scenario.snapshot = replace(scenario.snapshot, request=scenario.request)
    original = RefreshRules().model_inputs(
        scenario.snapshot, RefreshRules().chunk(scenario.snapshot)
    )[0]
    # 대체 계수는 글자 수 합이다. 첫 입력이 예산(max_input_tokens의 80%)에 꼭 맞게 한다.
    tokens = len(original.instructions) + len(original.prompt)
    limits = ModelLimits("gpt-5.6-luna", 10, tokens * 5 // 4 + 1, 2000, 1050000)
    assert limits.input_budget == tokens
    client = client_fixture()
    # 첫 응답은 JSON이 아니어서 진단이 붙은, 보완할 수 있는 실패가 된다.
    client.responses.create.return_value.output_text = "{"
    scenario.service.model = OpenAIProposalModel(client, limits)
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "MODEL_REQUEST_INVALID")
    assert result.failure.message == "Model input token budget exceeded"
    # 보완 입력도 계수했지만 예산을 넘어 생성 요청은 첫 번째 하나뿐이다.
    assert client.responses.input_tokens.count.call_count == 2
    client.responses.create.assert_called_once()
    assert result.usage == Usage(1, 10, 20)
    assert len(result.attempts) == 1


def test_aggregate_failure_with_diagnostics_is_not_repaired():
    class AggregateFailureRules(RefreshRules):
        def validate_candidate(self, snapshot, candidate, model_input):
            result = super().validate_candidate(snapshot, candidate, model_input)
            if model_input.relation_scope is None:
                assert result.outcome == Outcome.PROPOSED
                return replace(
                    result,
                    outcome=Outcome.FAILED,
                    document_proposals=(),
                    relation_proposals=(),
                    new_document_proposals=(),
                    facts=(),
                    failure=Failure("CONFLICTING_PROPOSALS", "Aggregate conflict"),
                    diagnostics=(
                        ValidationDiagnostic(
                            "document_proposals", "aggregate_conflict"
                        ),
                    ),
                )
            return result

    scenario = repair_scenario(
        target_ids=("haeju", "doyun"), rules=AggregateFailureRules()
    )
    scenario.model.responses.extend(
        (candidate_for(scenario), candidate_for(scenario, "doyun"))
    )
    _, result = run_saved(scenario)
    assert_atomic_failure(result, "CONFLICTING_PROPOSALS")
    assert len(scenario.model.inputs) == 2
    assert len(result.attempts) == 2
    assert all(a.outcome == Outcome.PROPOSED for a in result.attempts)
    assert result.diagnostics


@pytest.mark.parametrize("successful_repair", [False, True])
def test_saved_result_reuse_does_not_repeat_failed_or_repaired_calls(successful_repair):
    scenario = repair_scenario()
    first = invalid_identity(candidate_for(scenario))
    second = candidate_for(scenario)
    if not successful_repair:
        second = invalid_identity(second)
    scenario.model.responses.extend((first, second))
    if not successful_repair:
        scenario.model.responses.append(second)
    completion, result = run_saved(scenario)
    assert completion.outcome == (
        Outcome.PROPOSED if successful_repair else Outcome.FAILED
    )
    count = 2 if successful_repair else 3
    assert len(scenario.model.inputs) == count
    again = scenario.service.run(scenario.request)
    assert again == completion
    assert scenario.artifacts.read_result(scenario.request) == result
    assert len(scenario.model.inputs) == count
    assert len(scenario.inbox.pending()) == 1


def test_repair_can_return_valid_no_change_without_publishing_failed_proposals():
    scenario = repair_scenario()
    original = invalid_identity(candidate_for(scenario))
    unchanged = ModelCandidate((), usage=Usage(1, 5, 7))
    scenario.model.responses.extend((original, unchanged))
    _, result = run_saved(scenario)
    assert result.outcome == Outcome.NO_CHANGE
    assert not result.document_proposals
    assert not result.facts
    assert result.usage == Usage(2, 25, 37)
    assert result.attempts[1].outcome == Outcome.NO_CHANGE
