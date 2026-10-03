from dataclasses import replace

from app.refresh.models import DocumentBatch
from app.refresh.rules import RefreshRules
from evaluation.cases import assess, cases


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
            snapshot, case.expected, replace(inputs[0], target_ids=snapshot.target_ids)
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
