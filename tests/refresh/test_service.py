from dataclasses import replace

import pytest

from app.refresh.errors import RulesNotImplemented
from app.refresh.models import JobState, Outcome, Selection
from tests.fakes.scenario import scenario


def test_normal_flow_preserves_snapshots_and_orders_io():
    s = scenario()
    done = s.service().run(s.request, "execution-1")
    assert done.outcome == Outcome.PROPOSED
    assert s.artifacts.read_input(s.request) == s.source
    assert s.artifacts.read_context(s.request) == s.snapshot
    assert s.artifacts.read_result(s.request) == s.result
    assert s.jobs.load(s.request).state == JobState.COMPLETED
    assert s.publisher.delivered == [done]
    names = s.calls.names()
    assert names.index("relations.find_related") < names.index("documents.fetch_documents")
    assert names.index("artifacts.save_context") < names.index("model.generate")
    assert names.index("rules.validate_candidate") < names.index("artifacts.save_result")
    assert names.index("artifacts.save_result") < names.index("publisher.publish")
    fetched = next(args for name, args in s.calls.events if name == "documents.fetch_documents")
    assert fetched == ("project-1", ("setting-1",))
    assembled = next(args for name, args in s.calls.events if name == "rules.assemble")
    assert assembled[1] == s.source
    assert assembled[-1].documents == s.snapshot.documents[1:]
    validated = next(args for name, args in s.calls.events if name == "rules.validate_candidate")
    assert validated == (s.snapshot, s.model.candidate, s.rules.model_input)


def test_explicit_complete_input_skips_relations_and_content_queries():
    s = scenario()
    request = replace(s.request, discover_related=False, target_ids=("setting-1",))
    source = replace(s.source, documents=s.snapshot.documents)
    s.artifacts.inputs[request.input_ref] = source
    s.rules.snapshot = replace(s.snapshot, request=request)
    s.rules.selection = Selection(("draft-1", "setting-1"), ())
    s.service().run(request, "execution-1")
    assert "relations.find_related" not in s.calls.names()
    assert "documents.fetch_documents" not in s.calls.names()


def test_no_change_is_explicit_in_result_and_completion():
    s = scenario()
    s.model.candidate = replace(s.model.candidate, proposals=())
    s.rules.result = replace(s.result, outcome=Outcome.NO_CHANGE, proposals=())
    completion = s.service().run(s.request, "execution-1")
    assert completion.outcome == Outcome.NO_CHANGE
    assert s.artifacts.read_result(s.request).proposals == ()


def test_default_unimplemented_rules_never_publish_or_report_success():
    s = scenario()
    with pytest.raises(RulesNotImplemented, match="validate_input"):
        s.service(use_stub=False).run(s.request, "execution-1")
    assert s.publisher.delivered == []
    assert "model.generate" not in s.calls.names()
    assert s.artifacts.read_result(s.request) is None
    assert s.jobs.load(s.request).state == JobState.RETRYABLE
