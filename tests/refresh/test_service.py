from dataclasses import replace

from app.refresh.models import Failure, Outcome, Selection
from tests.fakes.scenario import scenario


def test_normal_flow_preserves_snapshots_and_orders_io():
    s = scenario()
    done = s.service().run(s.request)
    assert done.outcome == Outcome.PROPOSED
    assert s.artifacts.read_input(s.request) == s.source
    assert s.artifacts.read_context(s.request) == s.snapshot
    stored = s.artifacts.read_result(s.request)
    assert replace(stored, attempts=()) == s.result
    assert len(stored.attempts) == 1
    assert stored.attempts[0].outcome == Outcome.PROPOSED
    assert s.inbox.find(s.request) == done
    assert [entry.completion for entry in s.inbox.pending()] == [done]
    names = s.calls.names()
    assert names.index("relations.find_related") < names.index(
        "documents.fetch_documents"
    )
    assert names.index("artifacts.save_context") < names.index("model.generate")
    assert names.index("rules.validate_candidate") < names.index(
        "artifacts.save_result"
    )
    assert names.index("artifacts.save_result") < names.index("inbox.complete")
    fetched = next(
        args for name, args in s.calls.events if name == "documents.fetch_documents"
    )
    assert fetched == ("project-1", ("setting-1",))
    assembled = next(args for name, args in s.calls.events if name == "rules.assemble")
    assert assembled[1] == s.source
    assert assembled[-1].documents == s.snapshot.documents[1:]
    validated = next(
        args for name, args in s.calls.events if name == "rules.validate_candidate"
    )
    assert validated == (s.snapshot, s.model.candidate, s.rules.model_input)


def test_explicit_complete_input_skips_relations_and_content_queries():
    s = scenario()
    request = replace(s.request, discover_related=False, target_ids=("setting-1",))
    source = replace(s.source, documents=s.snapshot.documents)
    s.artifacts.inputs[request.input_ref] = source
    s.rules.snapshot = replace(s.snapshot, request=request)
    s.rules.selection = Selection(("draft-1", "setting-1"), ())
    s.service().run(request)
    assert "relations.find_related" not in s.calls.names()
    assert "documents.fetch_documents" not in s.calls.names()


def test_no_change_is_explicit_in_result_and_completion():
    s = scenario()
    s.model.candidate = replace(s.model.candidate, document_proposals=())
    s.rules.result = replace(s.result, outcome=Outcome.NO_CHANGE, document_proposals=())
    completion = s.service().run(s.request)
    assert completion.outcome == Outcome.NO_CHANGE
    assert s.artifacts.read_result(s.request).document_proposals == ()


def test_default_rules_validate_and_publish_a_real_proposal():
    s = scenario()
    # Exercise the historical factless fixture under its explicit read contract.
    s.request = replace(
        s.request,
        model_settings=replace(
            s.request.model_settings, schema_version="refresh-candidate-v3"
        ),
    )
    done = s.service(use_stub=False).run(s.request)
    assert done.outcome == Outcome.PROPOSED
    assert s.inbox.find(s.request) == done
    assert (
        s.artifacts.read_result(s.request).document_proposals
        == s.model.candidate.document_proposals
    )


def test_unprojected_request_fails_once_without_running():
    s = scenario()
    failure = Failure("PROJECTION_NOT_READY", "Projection has not caught up")
    done = s.service().fail(s.request, failure)
    assert done.outcome == Outcome.FAILED and done.failure == failure
    assert done.prompt_version == s.request.model_settings.prompt_version
    assert "model.generate" not in s.calls.names()
    assert "artifacts.read_input" not in s.calls.names()
    # 재전달되어 다시 실패시켜도 같은 완료 하나만 남는다.
    assert s.service().fail(s.request, failure) == done
    assert [entry.completion for entry in s.inbox.pending()] == [done]


def test_failing_an_already_saved_request_reuses_its_result():
    s = scenario()
    finished = s.service().run(s.request)
    late = s.service().fail(s.request, Failure("PROJECTION_NOT_READY", "late"))
    assert late == finished and late.outcome == Outcome.PROPOSED
