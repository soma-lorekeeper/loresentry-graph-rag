"""토큰 예산을 넘는 2단계 요청을 seed 묶음으로 나눠 누적하는 흐름을 확인한다."""

import json
from dataclasses import replace

import pytest

from app.adapters.llm_schema import parse_candidate
from app.refresh.batching import BatchState, batch_snapshot
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    ArtifactRef,
    DocumentBatch,
    DocumentProposal,
    DocumentSnapshot,
    Failure,
    InputSnapshot,
    JobKey,
    ModelSettings,
    Outcome,
    RefreshRequest,
    RelatedDocuments,
    Relation,
    Usage,
)
from app.refresh.rules import RefreshRules
from app.refresh.serialization import result_from_payload, result_to_payload
from app.refresh.service import RefreshService
from app.refresh.validation import existing_setting_names, setting_name
from app.refresh.workflow import PLAN_SCHEMA, build_plan_input
from tests.fakes.refresh import (
    Calls,
    FakeDocuments,
    FakeInbox,
    FakeJsonArtifacts,
    FakeRelations,
)

PROJECT = "batch-project"
OBJECTS = {"chapter-a": "강철 덮개", "chapter-b": "구리 손잡이"}


class ScriptedModel:
    """받은 묶음 문맥만 보고 도윤 문단 하나를 바꾸는 계획·작성 응답을 만든다."""

    def __init__(self, failures=()):
        self.inputs = []
        self.failures = dict(failures)  # 호출 순번(0부터) -> 던질 예외

    def generate(self, model_input):
        index = len(self.inputs)
        self.inputs.append(model_input)
        if index in self.failures:
            raise self.failures[index]
        context = json.loads(model_input.prompt)
        payload = (
            plan(context)
            if model_input.schema_version == PLAN_SCHEMA
            else written(context)
        )
        return parse_candidate(
            json.dumps(payload, ensure_ascii=False),
            Usage(1, 10, 20),
            schema_version=model_input.schema_version,
            model_input=model_input,
        )


def plan(context):
    marker = next(
        item["marker"]
        for item in context["evidence"]
        if item["document_id"] in context["changed_ids"] and "사용했다" in item["quote"]
    )
    paragraph = next(p for p in context["paragraphs"] if p["document_id"] == "doyun")
    return {
        "reviewed_document_ids": context["target_ids"],
        "edits": [
            {
                "edit_id": "E1",
                "document_id": "doyun",
                "base_revision_no": paragraph["base_revision_no"],
                "paragraph_id": paragraph["paragraph_id"],
                "operation": "REPLACE",
                "instruction": "사용한 물건을 덧붙이고 다른 서술은 보존한다.",
                "source_markers": [marker],
            }
        ],
        "new_documents": [],
        "relations": [],
    }


def written(context):
    edit = context["change_plan"]["edits"][0]
    marker = edit["source_markers"][0]
    source = next(item for item in context["evidence"] if item["marker"] == marker)
    used = OBJECTS[source["document_id"]]
    paragraph = next(
        p
        for p in context["paragraphs"]
        if (p["document_id"], p["paragraph_id"]) == ("doyun", edit["paragraph_id"])
    )
    return {
        "facts": [
            {
                "fact_id": "F1",
                "actor": {"surface": "백도윤", "document_id": "doyun"},
                "action": "사용했다",
                "recipient": None,
                "object": {"surface": used, "document_id": None},
                "certainty": "CONFIRMED",
                "extent": "UNSPECIFIED",
                "time": "PAST",
                "statement": f"백도윤은 {used}를 사용했다.",
                "source": {"marker": marker},
            }
        ],
        "paragraph_edits": [
            {
                "edit_id": "E1",
                "text": paragraph["text"].replace(
                    "배달부다.", f"{used}도 쓰는 배달부다."
                ),
                "fact_ids": ["F1"],
            }
        ],
        "new_document_proposals": [],
        "relation_proposals": [],
    }


class FakeTokens:
    """글자 수를 토큰 수로 쓰는 계수기. 실제 계수 API 대신 결정적으로 센다."""

    def __init__(self):
        self.counted = []

    def count(self, model_input):
        self.counted.append(model_input)
        return len(model_input.instructions) + len(model_input.prompt)


def manuscript(document_id, used):
    return DocumentSnapshot(
        PROJECT,
        document_id,
        2,
        f"기록 앞부분.\n백도윤은 오래 아껴 온 {used}를 사용했다.\n기록 뒷부분.",
        relations=(Relation(PROJECT, document_id, "doyun", "related_character"),),
        title=document_id,
    )


def scenario():
    request = RefreshRequest(
        JobKey(PROJECT, "request", "fingerprint"),
        ArtifactRef("bucket", "input"),
        ("chapter-a", "chapter-b"),
        discover_related=False,
        model_settings=ModelSettings(
            "fake", "refresh-prompt-v13", "refresh-candidate-v7"
        ),
    )
    source = InputSnapshot(
        request.job,
        tuple(manuscript(key, used) for key, used in OBJECTS.items()),
    )
    doyun = DocumentSnapshot(
        PROJECT,
        "doyun",
        4,
        "백도윤은 배달부다.",
        properties=(("name", "백도윤"), ("alias", "도윤")),
        folder_code="CHARACTER",
        title="백도윤",
    )
    rules = RefreshRules()
    related = RelatedDocuments()
    selection = rules.select(request, source, related)
    full = rules.assemble(request, source, related, selection, DocumentBatch((doyun,)))
    return request, source, doyun, full


def plan_size(full, seeds):
    rules = RefreshRules()
    batch = batch_snapshot(full, BatchState(), seeds)
    base = rules.model_inputs(batch, rules.chunk(batch))[0]
    planned = build_plan_input(batch, base)
    return len(planned.instructions) + len(planned.prompt)


def service(model, *, budget=None, artifacts=None, tokens=None):
    request, source, doyun, _ = scenario()
    calls = Calls()
    artifacts = artifacts or FakeJsonArtifacts({request.input_ref: source}, calls)
    return RefreshService(
        artifacts=artifacts,
        relations=FakeRelations(RelatedDocuments(), calls),
        documents=FakeDocuments((doyun,), calls),
        model=model,
        inbox=FakeInbox(calls=calls),
        tokens=tokens or (FakeTokens() if budget is not None else None),
        plan_budget=budget,
    )


def split_budget():
    """원고 하나씩은 들고 두 원고를 함께는 넘는 예산. 둘째 묶음은 고친 본문만큼 길다."""
    _, _, _, full = scenario()
    single = plan_size(full, ("chapter-a",))
    both = plan_size(full, ("chapter-a", "chapter-b"))
    assert both - single > 200
    return (single + both) // 2


def test_request_within_budget_runs_the_existing_two_stage_path():
    request = scenario()[0]
    plain = ScriptedModel()
    expected = service(plain).run(request)
    counted = ScriptedModel()
    tokens = FakeTokens()
    batched = service(counted, budget=10**9, tokens=tokens)
    assert batched.run(request) == expected
    assert counted.inputs == plain.inputs
    assert batched.artifacts.batches == {}
    assert len(tokens.counted) == 1


def test_two_batches_carry_the_edited_body_and_validate_once():
    request, _, _, full = scenario()
    model = ScriptedModel()
    refresh = service(model, budget=split_budget())
    assert refresh.run(request).outcome == Outcome.PROPOSED
    result = refresh.artifacts.read_result(request)
    (proposal,) = result.document_proposals
    # 둘째 묶음은 첫 묶음이 고친 본문 위에서 고치고, 기준 revision은 원래 값이다.
    assert proposal.value == "백도윤은 강철 덮개도 쓰는 구리 손잡이도 쓰는 배달부다."
    assert proposal.base_revision_no == 4
    assert {e.document_id for e in proposal.evidence} == {"chapter-a", "chapter-b"}
    assert len(result.facts) == 2
    second_plan = json.loads(model.inputs[2].prompt)
    assert second_plan["changed_ids"] == ["chapter-b"]
    assert (
        second_plan["settings_bodies"]["doyun"] == "백도윤은 강철 덮개도 쓰는 배달부다."
    )
    assert [a.call_index for a in result.attempts] == [1, 2, 3, 4]
    assert [a.stage for a in result.attempts] == ["PLAN", "WRITE"] * 2
    assert result.usage == Usage(4, 40, 80)
    assert result.change_plan is None
    assert sorted(refresh.artifacts.batches) == [
        ((PROJECT, "request"), 1),
        ((PROJECT, "request"), 2),
    ]
    restored = result_from_payload(
        json.loads(json.dumps(result_to_payload(request, result, full)))
    )
    assert restored == result


def test_packing_counts_the_whole_queue_then_bisects():
    request = scenario()[0]
    tokens = FakeTokens()
    refresh = service(ScriptedModel(), budget=split_budget(), tokens=tokens)
    assert refresh.run(request).outcome == Outcome.PROPOSED
    # 첫 묶음은 전체를 센 뒤 절반을 세고, 둘째 묶음은 남은 전체를 한 번 센다.
    assert [json.loads(i.prompt)["changed_ids"] for i in tokens.counted] == [
        ["chapter-a", "chapter-b"],
        ["chapter-a"],
        ["chapter-b"],
    ]


def test_redelivery_resumes_after_the_saved_batch():
    request = scenario()[0]
    # 둘째 묶음의 작성 호출이 재시도 가능하게 실패해 요청이 재전달되는 경우다.
    interrupted = ScriptedModel(
        {3: RefreshFailure(Failure("MODEL_UNAVAILABLE", "Retry model", True))}
    )
    first = service(interrupted, budget=split_budget())
    with pytest.raises(RefreshFailure):
        first.run(request)
    assert len(first.artifacts.batches) == 1
    resumed = ScriptedModel()
    second = service(resumed, budget=split_budget(), artifacts=first.artifacts)
    assert second.run(request).outcome == Outcome.PROPOSED
    # 저장된 첫 묶음은 모델 없이 다시 쓰고 둘째 묶음만 호출한다.
    assert [json.loads(i.prompt)["changed_ids"] for i in resumed.inputs] == [
        ["chapter-b"],
        ["chapter-b"],
    ]
    uninterrupted = service(ScriptedModel(), budget=split_budget())
    uninterrupted.run(request)
    assert second.artifacts.read_result(request) == uninterrupted.artifacts.read_result(
        request
    )


def test_a_failed_batch_fails_the_whole_request_with_earlier_attempts():
    request = scenario()[0]
    rejected = RefreshFailure(Failure("MODEL_REFUSED", "Model refused the request"))
    model = ScriptedModel({2: rejected})
    refresh = service(model, budget=split_budget())
    completion = refresh.run(request)
    assert completion.outcome == Outcome.FAILED
    result = refresh.artifacts.read_result(request)
    assert result.failure.code == "MODEL_REFUSED"
    assert result.document_proposals == ()
    assert [a.call_index for a in result.attempts] == [1, 2, 3]
    assert result.usage.calls == 3


def test_a_seed_with_its_neighbors_over_budget_fails_before_model_calls():
    request = scenario()[0]
    model = ScriptedModel()
    refresh = service(model, budget=100)
    assert refresh.run(request).outcome == Outcome.FAILED
    result = refresh.artifacts.read_result(request)
    assert result.failure.code == "CONTEXT_BUDGET_EXCEEDED"
    assert model.inputs == []


def test_batch_carries_neighbor_bodies_and_knows_settings_outside_it():
    _, _, _, full = scenario()
    state = BatchState()
    edited = "백도윤은 바뀐 배달부다."
    state.documents["doyun"] = DocumentProposal("doyun", 4, "body_text", edited, ())
    batch = batch_snapshot(full, state, ("chapter-a",))
    bodies = {d.document_id: d.body_text for d in batch.documents}
    # seed가 아닌 이웃은 앞 묶음이 고친 본문으로 보인다.
    assert bodies["doyun"] == edited
    assert batch.working_bodies == ()
    # 관계가 없어 묶음에 들지 않은 설정도 이름은 알아 같은 새 설정을 만들지 않는다.
    unrelated = replace(
        full, documents=tuple(replace(d, relations=()) for d in full.documents)
    )
    alone = batch_snapshot(unrelated, BatchState(), ("chapter-a",))
    assert "doyun" not in {d.document_id for d in alone.documents}
    assert ("CHARACTER", setting_name("도윤")) in existing_setting_names(alone)


def test_a_changed_setting_keeps_its_original_body_as_a_source():
    _, _, doyun, full = scenario()
    state = BatchState()
    edited = "백도윤은 바뀐 배달부다."
    state.documents["doyun"] = DocumentProposal("doyun", 4, "body_text", edited, ())
    seeded = replace(full, changed_ids=full.changed_ids + ("doyun",))
    batch = batch_snapshot(seeded, state, ("doyun",))
    # 근거 위치가 원래 revision을 가리키도록 seed 원문은 두고 현재 본문만 따로 싣는다.
    assert next(d for d in batch.documents if d.document_id == "doyun") == doyun
    assert batch.working_bodies == (("doyun", edited),)
