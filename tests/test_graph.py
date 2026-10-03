from copy import deepcopy

import pytest

from app.graph import GraphStatus, InvalidGraphStatus, interpret_status


def test_status_interpretation_is_repeatable_and_does_not_mutate_input():
    payload = {
        "role": "writer",
        "dbEngineVersion": "1.4.8.0",
        "gremlin": {"version": "3.7.1"},
        "extra": {"value": [1, 2]},
    }
    original = deepcopy(payload)
    expected = GraphStatus("writer", "1.4.8.0", "3.7.1")
    assert interpret_status(payload) == interpret_status(payload) == expected
    assert payload == original


@pytest.mark.parametrize("payload", [{}, {"gremlin": {}}, {"extra": "ignored"}])
def test_missing_metadata_is_unknown(payload):
    assert interpret_status(payload) == GraphStatus("unknown", "unknown", "unknown")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "ok",
        {"gremlin": None},
        {"gremlin": []},
        {"gremlin": "3.7.1"},
        {"role": None},
        {"role": 123},
        {"dbEngineVersion": False},
        {"gremlin": {"version": []}},
    ],
)
def test_malformed_metadata_is_rejected(payload):
    with pytest.raises(InvalidGraphStatus):
        interpret_status(payload)
