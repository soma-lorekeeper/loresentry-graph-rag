from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, ModelInput
from tests.fakes.fixtures import example
from tests.fakes.refresh import FakeModel


@pytest.mark.parametrize("variant", ["valid", "no_change", "bad_field", "bad_evidence"])
def test_model_returns_scripted_candidates_without_validating_them(variant):
    candidate = example()[4]
    if variant == "no_change":
        candidate = replace(candidate, proposals=())
    elif variant == "bad_field":
        candidate = replace(candidate, proposals=(replace(candidate.proposals[0], field="unsupported"),))
    elif variant == "bad_evidence":
        evidence = replace(candidate.proposals[0].evidence[0], document_id="missing")
        candidate = replace(candidate, proposals=(replace(candidate.proposals[0], evidence=(evidence,)),))
    model = FakeModel(candidate)
    model_input = ModelInput("fixed", "fixture", "v1", "fake")
    assert model.generate(model_input) == model.generate(model_input) == candidate
    assert model.calls.events == [("model.generate", (model_input,))] * 2
    assert FakeModel(candidate).calls.events == []


def test_model_timeout_is_scripted_and_retry_returns_same_candidate():
    candidate = example()[4]
    model = FakeModel(candidate)
    model.calls.failures["model.generate"].append(RefreshFailure(Failure("LLM_TIMEOUT", "timeout", True)))
    model_input = ModelInput("fixed", "fixture", "v1", "fake")
    with pytest.raises(RefreshFailure) as failure:
        model.generate(model_input)
    assert failure.value.failure.retryable
    assert model.generate(model_input) == candidate
