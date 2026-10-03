import inspect

import pytest

from app.refresh.errors import RulesNotImplemented
from app.refresh.rules import RefreshRules


@pytest.mark.parametrize(
    "method",
    [
        "validate_candidate",
    ],
)
def test_every_unimplemented_boundary_stops_instead_of_accepting_data(method):
    function = getattr(RefreshRules(), method)
    with pytest.raises(RulesNotImplemented, match=method):
        function(*[None for _ in inspect.signature(function).parameters])
