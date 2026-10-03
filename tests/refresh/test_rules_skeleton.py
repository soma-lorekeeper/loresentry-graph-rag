import inspect

import pytest

from app.refresh.errors import RulesNotImplemented
from app.refresh.models import ModelInput
from app.refresh.rules import RefreshRules


@pytest.mark.parametrize(
    "method",
    [
        "chunk",
        "build_context",
        "build_prompt",
        "validate_candidate",
    ],
)
def test_every_unimplemented_boundary_stops_instead_of_accepting_data(method):
    function = getattr(RefreshRules(), method)
    with pytest.raises(RulesNotImplemented, match=method):
        function(*[None for _ in inspect.signature(function).parameters])


def test_stub_requires_explicit_injection_and_does_not_change_default():
    class PromptStub(RefreshRules):
        def build_prompt(self, context):
            return ModelInput(context, "fixture", "fixture", "fake")

    assert PromptStub().build_prompt("fixed").model == "fake"
    with pytest.raises(RulesNotImplemented):
        RefreshRules().build_prompt("fixed")
