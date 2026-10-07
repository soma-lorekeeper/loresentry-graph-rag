import json
from dataclasses import asdict, replace

import pytest

from app.adapters.llm_schema import parse_candidate
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentBatch, ModelCandidate, Outcome, Usage
from app.refresh.rules import RefreshRules
from app.refresh.serialization import result_from_payload
from evaluation.cases import new_setting_cases
from evaluation.runner import CallBudget, assemble, run_case
from scripts.evaluate_refresh import main
from tests.refresh.test_evaluation_runner import arguments


def fixture(existing=True):
    case = new_setting_cases("gpt-5.6-luna", "test")[int(existing)]
    rules = RefreshRules()
    snapshot = rules.assemble(
        case.request,
        case.source,
        case.related,
        rules.select(case.request, case.source, case.related),
        DocumentBatch(case.targets),
    )
    model_input = rules.model_inputs(snapshot, rules.chunk(snapshot))[0]
    return case, rules, snapshot, model_input


@pytest.mark.parametrize("existing", [False, True])
def test_new_character_with_links_roundtrips_and_preserves_manuscript(existing):
    case, _, _, _ = fixture(existing)
    budget = CallBudget(1)
    scenario = assemble(case, None, budget)
    result, assessment = run_case(case, scenario, repeat_saved=True)
    assert result.outcome == Outcome.PROPOSED
    assert result.new_document_proposals == case.expected.new_document_proposals
    assert result.document_proposals == ()
    assert len(result.relation_proposals) == 1
    assert assessment["manuscript_content_preserved"]
    assert assessment["expected_new_settings"]
    assert budget.used == 1
    payload = json.loads(next(iter(scenario.artifacts.json_results.values())))
    assert payload["schema_version"] == "refresh-result-v2"
    assert result_from_payload(payload) == result
    refs = {e["evidence_id"] for e in payload["evidence"]}
    assert set(payload["new_document_proposals"][0]["evidence_refs"]) <= refs


def test_new_only_candidate_is_proposed_and_strict_parser_accepts_it():
    case, rules, snapshot, model_input = fixture(False)
    candidate = replace(case.expected, relation_proposals=())
    raw = asdict(candidate)
    raw.pop("usage")
    parsed = parse_candidate(json.dumps(raw), Usage(1))
    result = rules.validate_candidate(snapshot, parsed, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert result.new_document_proposals == candidate.new_document_proposals


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"folder_code": "MANUSCRIPT"}, "INVALID_NEW_DOCUMENT"),
        ({"folder_code": "UNKNOWN"}, "INVALID_NEW_DOCUMENT"),
        ({"candidate_id": "c1"}, "INVALID_NEW_DOCUMENT"),
        ({"name": ""}, "INVALID_NEW_DOCUMENT"),
        ({"body_text": ""}, "INVALID_NEW_DOCUMENT"),
        ({"body_text": "가" * 5001}, "INVALID_NEW_DOCUMENT"),
        ({"evidence": ()}, "INVALID_EVIDENCE"),
    ],
)
def test_bad_creation_discards_all_proposals(changes, code):
    case, rules, snapshot, model_input = fixture()
    candidate = replace(
        case.expected,
        new_document_proposals=(
            replace(case.expected.new_document_proposals[0], **changes),
        ),
    )
    result = rules.validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.FAILED
    assert result.failure.code == code
    assert not (
        result.document_proposals
        or result.new_document_proposals
        or result.relation_proposals
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"document_id": "new:CHARACTER:없는인물"}, "INVALID_RELATION_ENDPOINT"),
        ({"base_revision_no": 1}, "REVISION_MISMATCH"),
        ({"target_base_revision_no": None}, "REVISION_MISMATCH"),
        ({"relation_key": "related_item"}, "INVALID_RELATION_KEY"),
    ],
)
def test_bad_candidate_link_fails_whole_result(changes, code):
    case, rules, snapshot, model_input = fixture()
    candidate = replace(
        case.expected,
        relation_proposals=(replace(case.expected.relation_proposals[0], **changes),),
    )
    result = rules.validate_candidate(snapshot, candidate, model_input)
    assert result.failure.code == code
    assert result.new_document_proposals == ()


def test_new_to_new_relation_and_creation_deduplication():
    case, rules, snapshot, model_input = fixture(False)
    first = case.expected.new_document_proposals[0]
    second = replace(
        first, candidate_id="new:LOCATION:항구", folder_code="LOCATION", name="항구"
    )
    relation = replace(
        case.expected.relation_proposals[0],
        target_document_id=second.candidate_id,
        target_base_revision_no=None,
        relation_key="related_place",
    )
    candidate = ModelCandidate(
        (),
        new_document_proposals=(first, first, second),
        relation_proposals=(relation, relation),
    )
    result = rules.validate_candidate(snapshot, candidate, model_input)
    assert result.outcome == Outcome.PROPOSED
    assert len(result.new_document_proposals) == 2
    assert len(result.relation_proposals) == 1


def test_existing_name_and_conflicting_new_content_are_rejected():
    case, rules, snapshot, model_input = fixture()
    first = case.expected.new_document_proposals[0]
    for proposals, code in [
        (
            (replace(first, name="유나", candidate_id="new:CHARACTER:유나"),),
            "DUPLICATE_SETTING",
        ),
        ((first, replace(first, body_text="다른 설정")), "CONFLICTING_PROPOSALS"),
    ]:
        result = rules.validate_candidate(
            snapshot,
            replace(case.expected, new_document_proposals=proposals),
            model_input,
        )
        assert result.failure.code == code


def test_v2_parser_requires_creation_list():
    with pytest.raises(RefreshFailure):
        parse_candidate(
            '{"document_proposals": [], "relation_proposals": []}', Usage(1)
        )


def test_offline_new_settings_suite(tmp_path):
    assert (
        main(
            arguments(tmp_path / "new-settings")
            + ["--suite", "new-settings", "--repeat-saved"]
        )
        == 0
    )


@pytest.mark.parametrize("repeat_creation", [False, True])
def test_multi_call_creation_scope_and_aggregate(repeat_creation):
    from unittest.mock import Mock

    from app.refresh.models import Relation

    case, _, _, _ = fixture()
    second = replace(case.targets[0], document_id="c2", properties=(("name", "서연"),))
    case = replace(
        case,
        targets=case.targets + (second,),
        related=replace(
            case.related,
            document_ids=("c1", "c2"),
            relations=case.related.relations
            + (Relation("evaluation", "m1", "c2", "related_character"),),
        ),
    )
    delegate = Mock()
    delegate.generate.side_effect = [
        case.expected,
        case.expected if repeat_creation else ModelCandidate(()),
    ]
    scenario = assemble(case, delegate, CallBudget(2))
    result, _ = run_case(case, scenario)
    inputs = [call.args[0] for call in delegate.generate.call_args_list]
    assert [i.allow_new_documents for i in inputs] == [True, False]
    assert [json.loads(i.prompt)["allow_new_documents"] for i in inputs] == [
        True,
        False,
    ]
    if repeat_creation:
        assert result.failure.code == "INVALID_NEW_DOCUMENT"
        assert result.new_document_proposals == ()
    else:
        assert result.outcome == Outcome.PROPOSED
        assert result.new_document_proposals == case.expected.new_document_proposals
        assert len(result.relation_proposals) == 1
        assert result.usage.calls == 2


def test_new_setting_needs_changed_manuscript_evidence():
    case, rules, snapshot, model_input = fixture()
    setting = case.targets[0]
    from app.refresh.models import Evidence

    evidence = (
        Evidence(
            setting.document_id,
            setting.revision_no,
            0,
            len(setting.body_text),
            setting.body_text,
        ),
    )
    candidate = replace(
        case.expected,
        new_document_proposals=(
            replace(case.expected.new_document_proposals[0], evidence=evidence),
        ),
    )
    result = rules.validate_candidate(snapshot, candidate, model_input)
    assert result.failure.code == "INVALID_EVIDENCE"


def test_old_result_read_compatibility():
    from app.refresh.models import RefreshResult
    from app.refresh.serialization import result_to_payload

    case, _, snapshot, _ = fixture(False)
    payload = result_to_payload(
        case.request, RefreshResult(case.request.job, Outcome.NO_CHANGE), snapshot
    )
    payload["schema_version"] = "refresh-result-v1"
    payload.pop("new_document_proposals")
    restored = result_from_payload(payload)
    assert restored.new_document_proposals == ()
    assert restored.outcome == Outcome.NO_CHANGE
