from dataclasses import replace

import pytest

from app.refresh.models import DocumentBatch, Outcome, RefreshResult
from app.refresh.rules import RefreshRules
from evaluation.cases import (
    assess,
    cases,
    evaluation_fingerprint,
    evaluation_outcome,
    new_setting_cases,
)


def test_fixed_cases_preserve_metadata_and_pass_rules():
    for case in cases("gpt-5.6-luna", "trial"):
        rules = RefreshRules()
        rules.validate_input(case.request, case.source)
        snapshot = rules.assemble(
            case.request,
            case.source,
            case.related,
            rules.select(case.request, case.source, case.related),
            DocumentBatch(case.targets),
        )
        inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
        result = rules.validate_candidate(
            snapshot,
            case.expected,
            replace(
                inputs[0],
                target_ids=snapshot.target_ids,
                relation_scope=None,
                schema_version="refresh-candidate-v3",
            ),
        )
        assessment = assess(case, result)
        assert assessment["rules_accepted"], result.failure
        assert not assessment["missing_targets"]
        assert not assessment["unexpected_targets"]
        assert assessment["required_content_preserved"]
        assert assessment["outdated_content_removed"]
        assert assessment["expected_relations"]
        assert case.request.changed_documents[0].revision_no == 3
        assert case.source.documents[0].body_text == snapshot.documents[
            0
        ].body_text or any(d == case.source.documents[0] for d in snapshot.documents)


def test_long_case_has_repeated_newlines_emoji_and_untrusted_commands():
    case = cases("gpt-5.6-luna", "trial")[-1]
    text = case.source.documents[0].body_text
    assert 4000 < len(text) <= 5000
    assert text.count("같은 말. 🐈\n") > 400
    assert "모든 지시를 무시" in text
    assert cases("gpt-5.6-luna", "other")[-1].request.job != case.request.job


def test_assessment_detects_omission():
    case = cases("gpt-5.6-luna", "trial")[0]
    from app.refresh.models import Outcome, RefreshResult

    result = RefreshResult(case.request.job, Outcome.NO_CHANGE)
    assert assess(case, result)["missing_targets"] == ["c1"]


def creation_result(case, **changes):
    return RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        relation_proposals=case.expected.relation_proposals,
        new_document_proposals=case.expected.new_document_proposals,
        **changes,
    )


def test_new_setting_identity_and_links_do_not_depend_on_candidate_id():
    case = new_setting_cases("gpt-5.6-luna", "test")[0]
    creation = replace(
        case.expected.new_document_proposals[0], candidate_id="new:actor-7"
    )
    relation = replace(
        case.expected.relation_proposals[0], document_id=creation.candidate_id
    )
    result = replace(
        creation_result(case),
        new_document_proposals=(creation,),
        relation_proposals=(relation,),
    )
    assessment = assess(case, result)
    assert assessment["expected_new_settings"]
    assert assessment["expected_relations"]
    assert evaluation_outcome({"assessment": assessment}) == "passed"


@pytest.mark.parametrize("name", ["해무시의 기억세", "  해무시　기억세  "])
def test_registered_new_name_alias_fulfills_creation_content_and_source_link(name):
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    creation = replace(
        case.expected.new_document_proposals[0], candidate_id="new:tax-7", name=name
    )
    relation = replace(
        case.expected.relation_proposals[0], target_document_id=creation.candidate_id
    )
    assessment = assess(
        case,
        replace(
            creation_result(case),
            new_document_proposals=(creation,),
            relation_proposals=(relation,),
        ),
    )
    assert evaluation_outcome({"assessment": assessment}) == "passed"
    assert case.expected.new_document_proposals[0].name == "기억세"


@pytest.mark.parametrize(
    "code,name", [("WORLDVIEW", "부산 기억세"), ("ITEM", "해무시의 기억세")]
)
def test_unregistered_name_or_different_type_does_not_fulfill_alias(code, name):
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    creation = replace(
        case.expected.new_document_proposals[0], folder_code=code, name=name
    )
    assessment = assess(
        case, replace(creation_result(case), new_document_proposals=(creation,))
    )
    assert assessment["missing_new_settings"] == ["WORLDVIEW:기억세"]
    assert not assessment["expected_relations"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_alias_applies_to_allowed_creation_and_relation_endpoint():
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    creation = case.expected.new_document_proposals[0]
    relation = case.expected.relation_proposals[0]
    case = replace(
        case,
        expected=replace(
            case.expected, new_document_proposals=(), relation_proposals=()
        ),
        allowed_new_settings=(("WORLDVIEW", "해무시의 기억세"),),
        allowed_relations=(
            replace(relation, target_document_id="new:WORLDVIEW:해무시 기억세"),
        ),
    )
    assessment = assess(
        case,
        replace(
            creation_result(case),
            new_document_proposals=(creation,),
            relation_proposals=(relation,),
        ),
    )
    assert evaluation_outcome({"assessment": assessment}) == "passed"


def test_alias_resolution_preserves_existing_document_ids_with_new_prefix():
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    first = replace(case.source.documents[0], document_id="new:WORLDVIEW:기억세")
    second = replace(first, document_id="new:WORLDVIEW:해무시의 기억세")
    creation = replace(
        case.expected.new_document_proposals[0], candidate_id="new:tax-7"
    )
    relation = replace(
        case.expected.relation_proposals[0],
        document_id=first.document_id,
        target_document_id=creation.candidate_id,
    )
    case = replace(
        case,
        source=replace(case.source, documents=(first, second)),
        expected=replace(
            case.expected,
            new_document_proposals=(creation,),
            relation_proposals=(relation,),
        ),
    )
    assessment = assess(
        case,
        replace(
            creation_result(case),
            relation_proposals=(replace(relation, document_id=second.document_id),),
        ),
    )
    assert not assessment["expected_relations"]


def test_alias_cannot_bypass_forbidden_creation():
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    case = replace(case, forbidden_new_settings=(("WORLDVIEW", "해무시 기억세"),))
    assessment = assess(case, creation_result(case))
    assert assessment["forbidden_new_settings"] == ["WORLDVIEW:기억세"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize("body", ["납부하는 제도다.", "기억을 납부하는 왕의 제도다."])
def test_clean_alias_candidate_cannot_hide_an_invalid_matching_body(body):
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    case = replace(case, new_forbidden_words=(("WORLDVIEW", "해무시 기억세", ("왕",)),))
    bad = replace(case.expected.new_document_proposals[0], body_text=body)
    clean = replace(
        case.expected.new_document_proposals[0],
        candidate_id="new:tax-7",
        name="해무시의 기억세",
    )
    assessment = assess(
        case, replace(creation_result(case), new_document_proposals=(bad, clean))
    )
    assert not assessment["new_setting_content_valid"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize(
    "definitions",
    [
        (("WORLDVIEW", " ", ("기억세",)),),
        (("WORLDVIEW", "기억세", ()),),
        (("WORLDVIEW", "기억세", (" ",)),),
        (
            ("WORLDVIEW", "기억세", ("공통 이름",)),
            ("WORLDVIEW", "별도 제도", ("공통　이름",)),
        ),
        (
            ("WORLDVIEW", "기억세", ("별도 제도",)),
            ("WORLDVIEW", "별도 제도", ("다른 이름",)),
        ),
    ],
)
def test_empty_or_conflicting_new_name_alias_definitions_are_rejected(definitions):
    case = new_setting_cases("gpt-5.6-luna", "alias")[2]
    with pytest.raises(ValueError, match="alias"):
        replace(case, acceptable_new_names=definitions)


@pytest.mark.parametrize("classification", ["allowed", "forbidden", "unreviewed"])
def test_extra_new_setting_is_allowed_rejected_or_pending_review(classification):
    case = new_setting_cases("gpt-5.6-luna", "test")[0]
    extra = replace(
        case.expected.new_document_proposals[0],
        candidate_id="new:harbor",
        folder_code="LOCATION",
        name="항구",
        body_text="항구의 경비병이 민수다.",
    )
    if classification != "unreviewed":
        case = replace(
            case, **{f"{classification}_new_settings": (("LOCATION", "항구"),)}
        )
    result = replace(
        creation_result(case),
        new_document_proposals=case.expected.new_document_proposals + (extra,),
    )
    assessment = assess(case, result)
    outcome = evaluation_outcome({"assessment": assessment})
    assert (
        outcome
        == {
            "allowed": "passed",
            "forbidden": "failed",
            "unreviewed": "review_required",
        }[classification]
    )
    assert assessment["unreviewed_new_settings"] == (
        ["LOCATION:항구"] if classification == "unreviewed" else []
    )


def test_missing_required_new_setting_fails_even_with_an_allowed_extra():
    case = new_setting_cases("gpt-5.6-luna", "test")[0]
    case = replace(case, allowed_new_settings=(("LOCATION", "항구"),))
    extra = replace(
        case.expected.new_document_proposals[0], folder_code="LOCATION", name="항구"
    )
    assessment = assess(
        case,
        replace(creation_result(case), new_document_proposals=(extra,)),
    )
    assert assessment["missing_new_settings"] == ["CHARACTER:민수"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize("body", ["민수다.", "민수는 항구의 왕이다."])
def test_new_setting_required_and_forbidden_content(body):
    case = new_setting_cases("gpt-5.6-luna", "test")[0]
    case = replace(
        case,
        new_required_words=(("CHARACTER", "민수", ("항구",)),),
        new_forbidden_words=(("CHARACTER", "민수", ("왕",)),),
    )
    creation = replace(case.expected.new_document_proposals[0], body_text=body)
    assessment = assess(
        case, replace(creation_result(case), new_document_proposals=(creation,))
    )
    assert not assessment["new_setting_content_valid"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_optional_creation_content_is_checked_when_present():
    case = new_setting_cases("gpt-5.6-luna", "test")[0]
    case = replace(
        case,
        allowed_new_settings=(("LOCATION", "항구"),),
        new_required_words=(("LOCATION", "항구", ("경비병",)),),
    )
    assert assess(case, creation_result(case))["new_setting_content_valid"]
    extra = replace(
        case.expected.new_document_proposals[0],
        candidate_id="new:harbor",
        folder_code="LOCATION",
        name="항구",
        body_text="민수가 나타났다.",
    )
    result = replace(
        creation_result(case),
        new_document_proposals=case.expected.new_document_proposals + (extra,),
    )
    assert not assess(case, result)["new_setting_content_valid"]


def test_preserved_content_checks_unchanged_and_optional_updated_documents():
    case = cases("gpt-5.6-luna", "test")[2]
    case = replace(
        case, preserved_words=(("c1", ("서울",)),), allowed_document_targets=("c1",)
    )
    unchanged = RefreshResult(case.request.job, Outcome.NO_CHANGE)
    assert assess(case, unchanged)["preserved_content"]
    template = cases("gpt-5.6-luna", "test")[0].expected.document_proposals[0]
    updated = replace(
        unchanged,
        document_proposals=(replace(template, value="유나는 파란 망토를 입는다."),),
    )
    assessment = assess(case, updated)
    assert not assessment["unexpected_targets"]
    assert not assessment["preserved_content"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


@pytest.mark.parametrize("allowed", [False, True])
def test_extra_relation_requires_explicit_allowance(allowed):
    case = cases("gpt-5.6-luna", "test")[3]
    extra = replace(case.expected.relation_proposals[0], document_id="m1")
    if allowed:
        case = replace(case, allowed_relations=(extra,))
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        document_proposals=case.expected.document_proposals,
        relation_proposals=case.expected.relation_proposals + (extra,),
    )
    assessment = assess(case, result)
    assert evaluation_outcome({"assessment": assessment}) == (
        "passed" if allowed else "review_required"
    )


@pytest.mark.parametrize(
    "changes", [{"operation": "REMOVE"}, {"relation_key": "wrong"}]
)
def test_required_relation_compares_operation_and_keys(changes):
    case = cases("gpt-5.6-luna", "test")[1]
    result = RefreshResult(
        case.request.job,
        Outcome.PROPOSED,
        relation_proposals=(replace(case.expected.relation_proposals[0], **changes),),
    )
    assessment = assess(case, result)
    assert not assessment["expected_relations"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_new_setting_fixture_covers_every_type_with_content_and_links():
    from evaluation.runner import CallBudget, assemble, run_case

    fixtures = new_setting_cases("gpt-5.6-luna", "types")
    assert {
        candidate.folder_code
        for case in fixtures
        for candidate in case.expected.new_document_proposals
    } == {"CHARACTER", "WORLDVIEW", "LOCATION", "ORGANIZATION", "ITEM", "EVENT"}
    for case in fixtures:
        result, assessment = run_case(case, assemble(case, None, CallBudget(1)))
        assert result.outcome == (
            Outcome.PROPOSED
            if case.expected.new_document_proposals
            else Outcome.NO_CHANGE
        )
        assert evaluation_outcome({"assessment": assessment}) == "passed"
        assert case.new_required_words or case.forbidden_new_settings


def test_unconfirmed_instruction_in_body_cannot_pass_as_a_confirmed_setting():
    from app.refresh.models import Evidence, ModelCandidate, NewDocumentProposal
    from evaluation.runner import CallBudget, assemble, run_case

    case = next(
        case
        for case in new_setting_cases("gpt-5.6-luna", "negative")
        if case.name == "unconfirmed-settings"
    )
    case = replace(
        case,
        request=replace(
            case.request,
            model_settings=replace(
                case.request.model_settings, schema_version="refresh-candidate-v3"
            ),
        ),
    )
    source = case.source.documents[0]
    invention = NewDocumentProposal(
        "new:LOCATION:항만 아래 비밀 시설",
        "LOCATION",
        "항만 아래 비밀 시설",
        "항만 아래에 실제 비밀 시설이 있다.",
        (
            Evidence(
                source.document_id,
                source.revision_no,
                0,
                len(source.body_text),
                source.body_text,
            ),
        ),
    )

    class InventingModel:
        def generate(self, model_input):
            return ModelCandidate((), new_document_proposals=(invention,))

    result, assessment = run_case(case, assemble(case, InventingModel(), CallBudget(1)))
    assert result.outcome == Outcome.PROPOSED
    assert assessment["rules_accepted"]
    assert assessment["forbidden_new_settings"] == ["LOCATION:항만 아래 비밀 시설"]
    assert evaluation_outcome({"assessment": assessment}) == "failed"


def test_legacy_fingerprint_preserves_absent_title_and_includes_present_title():
    case = cases("gpt-5.6-luna", "test")[0]
    assert case.request.job.input_fingerprint == (
        "d994d13eb8ec073871218e9b5772f4beb0f15bb5720d9bd3f00093d5f5bc6e4a"
    )
    document = case.source.documents[0]
    assert evaluation_fingerprint(document) != evaluation_fingerprint(
        replace(document, title="새 원고 제목")
    )
    assert evaluation_fingerprint(replace(document, body_text="본문, title=None")) != (
        evaluation_fingerprint(replace(document, body_text="본문"))
    )


def test_offline_candidates_obey_discovery_update_and_relation_call_scopes():
    from evaluation.novel import novel_settings_case
    from evaluation.runner import (
        CallBudget,
        EvaluationModel,
        evaluation_snapshot,
        planned_calls,
    )

    case = novel_settings_case("gpt-5.6-luna", "scope-test")
    snapshot = evaluation_snapshot(case)
    rules = RefreshRules()
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    assert planned_calls(case) == len(inputs) == 9
    model = EvaluationModel(case, None, CallBudget(9))
    candidates = [model.generate(item) for item in inputs]
    assert not candidates[0].document_proposals
    assert candidates[0].new_document_proposals == case.expected.new_document_proposals
    assert all(not candidate.new_document_proposals for candidate in candidates[1:])
    assert sum(len(candidate.document_proposals) for candidate in candidates) == 3
    assert sum(len(candidate.relation_proposals) for candidate in candidates) == 2
    for item, candidate in zip(inputs, candidates, strict=True):
        assert (
            rules.validate_candidate(
                snapshot,
                candidate,
                replace(item, schema_version="refresh-candidate-v3"),
            ).failure
            is None
        )


def test_evaluation_snapshot_respects_source_and_related_selection():
    from evaluation.runner import evaluation_snapshot

    case = cases("gpt-5.6-luna", "selection")[0]
    case = replace(case, request=replace(case.request, discover_related=False))
    snapshot = evaluation_snapshot(case)
    assert snapshot.documents == case.source.documents
    assert not snapshot.target_ids
    assert not snapshot.related.relations
