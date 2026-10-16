"""같은 변경의 문서별 교체 계획과 서버 조립, 누락·예산·복구를 검증한다."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.adapters.llm_schema import candidate_schema, parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentSnapshot,
    InputSnapshot,
    ModelSettings,
    Outcome,
    Usage,
)
from app.refresh.rules import RefreshRules
from app.refresh.serialization import result_from_payload, result_to_payload
from app.refresh.service import RefreshService
from app.refresh.workflow import (
    build_plan_input,
    build_write_input,
    paragraphs,
    validate_plan,
)
from tests.fakes.refresh import (
    Calls,
    FakeDocuments,
    FakeInbox,
    FakeJsonArtifacts,
    FakeRelations,
)
from tests.refresh.test_semantic_relations import semantic_fixture


class WireModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.inputs = []

    def generate(self, model_input):
        self.inputs.append(model_input)
        item = self.responses.pop(0)
        if callable(item):
            item = item(model_input)
        if isinstance(item, Exception):
            raise item
        return parse_candidate(
            json.dumps(item, ensure_ascii=False),
            Usage(1, 10, 20),
            schema_version=model_input.schema_version,
            model_input=model_input,
        )


def fixture(*, maximum=3, confidence=False):
    _, _, original = semantic_fixture("백도윤은 검게 산화시킨 강철 덮개를 사용했다.")
    request = replace(
        original.request,
        target_ids=("doyun", "item"),
        model_settings=ModelSettings(
            "gpt-6-luna",
            "refresh-prompt-v13",
            "refresh-candidate-v8" if confidence else "refresh-candidate-v7",
        ),
        settings=replace(original.request.settings, max_model_calls=maximum),
    )
    doy = replace(
        original.documents[2],
        body_text="백도윤은 황동 덮개 의지를 사용한다. 배달부다.\n\n파견 기한은 한 달이다.  😀\n",
    )
    item = DocumentSnapshot(
        request.job.project_id,
        "item",
        8,
        "검지 의지의 덮개는 황동이며 교체할 수 있다.\n\n안쪽 나사축은 길다.",
        folder_code="ITEM",
        title="검지 의지",
    )
    source = InputSnapshot(request.job, original.documents[:1])
    rules = RefreshRules()
    selection = rules.select(request, source, original.related)
    from app.refresh.models import DocumentBatch

    snapshot = rules.assemble(
        request, source, original.related, selection, DocumentBatch((doy, item))
    )
    base = rules.model_inputs(snapshot, rules.chunk(snapshot))[0]
    plan_input = build_plan_input(snapshot, base)
    context = json.loads(plan_input.prompt)
    marker = next(
        item["marker"] for item in context["evidence"] if "강철 덮개" in item["quote"]
    )
    plan = {
        "reviewed_document_ids": list(snapshot.target_ids),
        "edits": [
            dict(
                edit_id=f"E{i}",
                document_id=d.document_id,
                base_revision_no=d.revision_no,
                paragraph_id="P0001",
                operation="REPLACE",
                instruction="덮개를 강철로 교체하고 다른 속성은 보존한다.",
                source_markers=[marker],
            )
            for i, d in enumerate((doy, item), 1)
        ],
        "new_documents": [],
        "relations": [],
    }
    fact = dict(
        fact_id="F1",
        actor={"surface": "백도윤", "document_id": "doyun"},
        action="사용했다",
        recipient=None,
        object={"surface": "강철 덮개", "document_id": None},
        certainty="CONFIRMED",
        extent="UNSPECIFIED",
        time="PAST",
        statement="백도윤은 강철 덮개를 사용했다.",
        source={"marker": marker},
    )
    if confidence:
        fact["confidence"] = 0.93
    written = {
        "facts": [fact],
        "paragraph_edits": [
            {
                "edit_id": "E1",
                "text": "백도윤은 강철 덮개 의지를 사용한다. 배달부다.",
                "fact_ids": ["F1"],
            },
            {
                "edit_id": "E2",
                "text": "검지 의지의 덮개는 강철이며 교체할 수 있다.",
                "fact_ids": ["F1"],
            },
        ],
        "new_document_proposals": [],
        "relation_proposals": [],
    }
    calls = Calls()
    artifacts = FakeJsonArtifacts({request.input_ref: source}, calls)
    model = WireModel([plan, written])
    service = RefreshService(
        artifacts=artifacts,
        relations=FakeRelations(original.related, calls),
        documents=FakeDocuments((doy, item), calls),
        model=model,
        inbox=FakeInbox(calls=calls),
        rules=rules,
    )
    return SimpleNamespace(
        request=request,
        snapshot=snapshot,
        base=base,
        plan_input=plan_input,
        plan=plan,
        written=written,
        model=model,
        artifacts=artifacts,
        service=service,
        doy=doy,
        item=item,
    )


def get_plan(s):
    return parse_candidate(
        json.dumps(s.plan),
        Usage(1, 10, 20),
        schema_version=s.plan_input.schema_version,
        model_input=s.plan_input,
    )


def test_two_calls_update_both_documents_and_preserve_unedited_bytes():
    s = fixture()
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    result = s.artifacts.read_result(s.request)
    assert result.usage == Usage(2, 20, 40)
    assert [attempt.stage for attempt in result.attempts] == ["PLAN", "WRITE"]
    proposals = {p.target_document_id: p for p in result.document_proposals}
    assert "황동" not in proposals["doyun"].value
    assert "황동" not in proposals["item"].value
    assert proposals["doyun"].value.endswith("\n\n파견 기한은 한 달이다.  😀\n")
    assert proposals["item"].value.endswith("\n\n안쪽 나사축은 길다.")
    assert result.change_plan == get_plan(s)
    restored = result_from_payload(
        json.loads(json.dumps(result_to_payload(s.request, result, s.snapshot)))
    )
    assert restored == result
    s.service.run(s.request)
    assert len(s.model.inputs) == 2


@pytest.mark.parametrize(
    "change",
    [
        "unknown_id",
        "wrong_revision",
        "unknown_marker",
        "missing_review",
        "duplicate_edit",
        "manuscript",
    ],
)
def test_invalid_plan_is_rejected_before_writing(change):
    s = fixture()
    if change == "unknown_id":
        s.plan["edits"][0]["paragraph_id"] = "P9999"
    elif change == "wrong_revision":
        s.plan["edits"][0]["base_revision_no"] += 1
    elif change == "unknown_marker":
        s.plan["edits"][0]["source_markers"] = ["C9999"]
    elif change == "missing_review":
        s.plan["reviewed_document_ids"].pop()
    elif change == "duplicate_edit":
        s.plan["edits"].append(s.plan["edits"][0])
    else:
        s.plan["edits"][0]["document_id"] = "chapter"
    with pytest.raises(RefreshFailure):
        validate_plan(s.snapshot, get_plan(s), s.plan_input)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "duplicate",
        "empty",
        "unchanged",
        "unknown_fact",
        "duplicate_fact",
    ],
)
def test_incomplete_or_invalid_written_edits_fail(change):
    s = fixture()
    write_input = build_write_input(s.snapshot, s.base, get_plan(s))
    if change == "missing":
        s.written["paragraph_edits"].pop()
    elif change == "extra":
        s.written["paragraph_edits"].append(
            {**s.written["paragraph_edits"][0], "edit_id": "E999"}
        )
    elif change == "duplicate":
        s.written["paragraph_edits"].append(s.written["paragraph_edits"][0])
    elif change == "empty":
        s.written["paragraph_edits"][0]["text"] = ""
    elif change == "unchanged":
        s.written["paragraph_edits"][0]["text"] = paragraphs(s.snapshot)[0].text
    elif change == "unknown_fact":
        s.written["paragraph_edits"][0]["fact_ids"] = ["F99"]
    else:
        s.written["paragraph_edits"][0]["fact_ids"] = ["F1", "F1"]
    with pytest.raises(RefreshFailure):
        parse_candidate(
            json.dumps(s.written),
            Usage(1),
            schema_version=write_input.schema_version,
            model_input=write_input,
        )


def test_one_repair_can_complete_missing_edit_within_three_calls():
    s = fixture()
    incomplete = {**s.written, "paragraph_edits": s.written["paragraph_edits"][:1]}
    s.model.responses = [s.plan, incomplete, s.written]
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    result = s.artifacts.read_result(s.request)
    assert result.usage.calls == 3
    assert [a.outcome for a in result.attempts] == [
        Outcome.PROPOSED,
        Outcome.FAILED,
        Outcome.PROPOSED,
    ]
    assert [a.call_index for a in result.attempts] == [1, 2, 2]


def test_plan_repair_reserves_write_call_and_does_not_allow_fourth():
    s = fixture()
    invalid = {**s.plan, "reviewed_document_ids": []}
    s.model.responses = [invalid, s.plan, s.written]
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    assert len(s.model.inputs) == 3
    assert [a.call_index for a in s.artifacts.read_result(s.request).attempts] == [
        1,
        1,
        2,
    ]


def test_budget_rejected_before_paid_calls():
    s = fixture()
    request = replace(
        s.request, settings=replace(s.request.settings, max_model_calls=1)
    )
    assert s.service.run(request).outcome == Outcome.FAILED
    assert not s.model.inputs


def test_confidence_uses_v8_and_is_stored():
    s = fixture(confidence=True)
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    assert s.artifacts.read_result(s.request).facts[0].confidence == 0.93
    assert (
        "confidence"
        in candidate_schema("refresh-candidate-v8")["$defs"]["ConfidenceSourceFactDTO"][
            "required"
        ]
    )


def test_delete_and_append_preserve_all_other_text():
    s = fixture()
    s.plan["edits"][0]["operation"] = "DELETE"
    s.written["paragraph_edits"][0]["text"] = ""
    s.plan["edits"][1]["operation"] = "APPEND"
    s.written["paragraph_edits"][1]["text"] = (
        "새 속성"  # 서버는 생성 텍스트를 임의로 정규화하지 않는다.
    )
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    proposals = {
        p.target_document_id: p.value
        for p in s.artifacts.read_result(s.request).document_proposals
    }
    assert proposals["doyun"] == "\n\n파견 기한은 한 달이다.  😀\n"
    assert (
        proposals["item"]
        == "검지 의지의 덮개는 황동이며 교체할 수 있다.\n\n새 속성\n\n안쪽 나사축은 길다."
    )


def test_writing_cannot_add_unplanned_new_settings_or_relationships():
    s = fixture()
    write_input = build_write_input(s.snapshot, s.base, get_plan(s))
    marker = s.plan["edits"][0]["source_markers"][0]
    s.written["new_document_proposals"] = [
        {
            "candidate_id": "new:ITEM:새 물건",
            "folder_code": "ITEM",
            "name": "새 물건",
            "body_text": "새 물건이다.",
            "evidence": [{"marker": marker}],
            "fact_ids": ["F1"],
        }
    ]
    with pytest.raises(RefreshFailure, match="Every planned new setting"):
        parse_candidate(
            json.dumps(s.written),
            Usage(1),
            schema_version=write_input.schema_version,
            model_input=write_input,
        )


def test_two_stages_reach_sdk_with_correct_response_schemas():
    from app.adapters.llm import OpenAIProposalModel
    from tests.refresh.test_llm import client_fixture, limits

    s = fixture(confidence=True)
    client = client_fixture()
    response = client.responses.create.return_value
    client.responses.create.side_effect = [
        replace_response(response, json.dumps(s.plan)),
        replace_response(response, json.dumps(s.written)),
    ]
    s.service.model = OpenAIProposalModel(
        client,
        replace(
            limits("gpt-6-luna"),
            max_input_tokens=120000,
            max_output_tokens=32768,
            context_window=152768,
        ),
    )
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    calls = client.responses.create.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["text"]["format"]["schema"]["required"] == [
        "reviewed_document_ids",
        "edits",
        "new_documents",
        "relations",
    ]
    assert "paragraph_edits" in calls[1].kwargs["text"]["format"]["schema"]["required"]
    assert all(call.kwargs["store"] is False for call in calls)


def replace_response(response, text):
    from copy import deepcopy

    result = deepcopy(response)
    result.output_text = text
    return result


def test_no_fourth_call_after_plan_repair_and_invalid_write():
    s = fixture()
    s.model.responses = [
        {**s.plan, "reviewed_document_ids": []},
        s.plan,
        {**s.written, "paragraph_edits": []},
    ]
    assert s.service.run(s.request).outcome == Outcome.FAILED
    result = s.artifacts.read_result(s.request)
    assert len(s.model.inputs) == 3
    assert result.usage.calls == 3
    assert result.change_plan is not None


def test_evaluation_records_two_stages_without_counting_plan_as_fact_observation(
    tmp_path,
):
    from app.adapters.llm import ModelLimits
    from app.refresh.models import ModelCandidate, RelatedDocuments
    from evaluation.artifacts import AttemptRecorder
    from evaluation.cases import EvaluationCase
    from evaluation.confidence import confidence_summary
    from evaluation.runner import CallBudget, assemble, planned_calls, run_case
    from evaluation.source_observation import source_summary

    s = fixture(confidence=True)
    case = EvaluationCase(
        "staged",
        s.request,
        s.artifacts.read_input(s.request),
        RelatedDocuments(),
        (s.doy, s.item),
        ModelCandidate(()),
        allowed_document_targets=("doyun", "item"),
    )
    recorder = AttemptRecorder(
        tmp_path,
        case,
        ModelLimits("gpt-6-luna", 300, 120000, 32768, 152768),
        False,
        observe_sources=True,
        observe_confidence=True,
    )
    scenario = assemble(case, s.model, CallBudget(3), recorder)
    assert planned_calls(case) == 2
    result, assessment = run_case(case, scenario, repeat_saved=True)
    assert result.outcome == Outcome.PROPOSED
    assert [record["stage"] for record in recorder.records] == ["PLAN", "WRITE"]
    assert recorder.records[0]["source_observation"]["status"] == "not_applicable"
    assert recorder.records[1]["source_observation"]["status"] == "observed"
    report = {"case": "staged", "observed_attempts": recorder.records}
    assert source_summary([report])["groups"]["initial"]["attempts"] == 1
    assert confidence_summary([report])["groups"]["initial_effective"]["attempts"] == 1


def test_additional_current_manuscript_marker_is_recorded_without_changing_edit_scope():
    from app.refresh.workflow import build_plan_input, build_write_input

    s = fixture()
    original = next(d for d in s.snapshot.documents if d.document_id == "chapter")
    extra = replace(original, document_id="chapter-extra")
    snapshot = replace(
        s.snapshot,
        documents=s.snapshot.documents + (extra,),
        changed_ids=s.snapshot.changed_ids + (extra.document_id,),
    )
    rules = RefreshRules()
    base = rules.model_inputs(snapshot, rules.chunk(snapshot))[0]
    planning = build_plan_input(snapshot, base)
    plan = parse_candidate(
        json.dumps(s.plan),
        Usage(1),
        schema_version=planning.schema_version,
        model_input=planning,
    )
    writing = build_write_input(snapshot, base, plan)
    context = json.loads(writing.prompt)
    marker = next(
        item["marker"]
        for item in context["evidence"]
        if item["document_id"] == extra.document_id
    )
    s.written["facts"][0]["source"]["marker"] = marker
    candidate = parse_candidate(
        json.dumps(s.written),
        Usage(1),
        schema_version=writing.schema_version,
        model_input=writing,
    )
    assert [
        (item.plan_item_id, item.source_markers)
        for item in candidate.source_scope_extensions
    ] == [("E1", (marker,)), ("E2", (marker,))]
    result = rules.validate_candidate(snapshot, candidate, writing)
    assert result.outcome == Outcome.PROPOSED
    result = replace(result, change_plan=plan)
    stored = result_from_payload(
        json.loads(json.dumps(result_to_payload(s.request, result, snapshot)))
    )
    assert stored.source_scope_extensions == candidate.source_scope_extensions


def test_existing_setting_marker_is_not_new_change_evidence():
    s = fixture()
    write_input = build_write_input(s.snapshot, s.base, get_plan(s))
    context = json.loads(write_input.prompt)
    marker = next(
        item["marker"] for item in context["evidence"] if item["document_id"] == "doyun"
    )
    s.written["facts"][0]["source"]["marker"] = marker
    with pytest.raises(RefreshFailure, match="current changed manuscripts"):
        parse_candidate(
            json.dumps(s.written),
            Usage(1),
            schema_version=write_input.schema_version,
            model_input=write_input,
        )


def test_full_copies_of_preserved_paragraphs_are_removed_and_audited():
    s = fixture()
    unchanged = "파견 기한은 한 달이다.  😀"
    original_text = unchanged + "\n\n" + s.written["paragraph_edits"][0]["text"]
    s.written["paragraph_edits"][0]["text"] = original_text
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    result = s.artifacts.read_result(s.request)
    body = next(
        p.value for p in result.document_proposals if p.target_document_id == "doyun"
    )
    assert body.count(unchanged) == 1
    assert result.paragraph_copy_removals[0].edit_id == "E1"
    assert result.paragraph_copy_removals[0].paragraph_ids == ("P0002",)
    assert result.paragraph_copy_removals[0].original_text == original_text
    stored = result_from_payload(
        json.loads(json.dumps(result_to_payload(s.request, result, s.snapshot)))
    )
    assert stored.paragraph_copy_removals == result.paragraph_copy_removals


def test_append_omits_anchor_copy_but_keeps_its_new_content():
    s = fixture()
    s.plan["edits"][1]["operation"] = "APPEND"
    anchor = "검지 의지의 덮개는 황동이며 교체할 수 있다."
    s.written["paragraph_edits"][1]["text"] = anchor + "\n\n새 점검 내용이다."
    assert s.service.run(s.request).outcome == Outcome.PROPOSED
    result = s.artifacts.read_result(s.request)
    body = next(
        p.value for p in result.document_proposals if p.target_document_id == "item"
    )
    assert body.count(anchor) == 1
    assert "새 점검 내용이다." in body
    assert result.paragraph_copy_removals[0].edit_id == "E2"
