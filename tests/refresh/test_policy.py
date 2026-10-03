from dataclasses import FrozenInstanceError

import pytest

from app.refresh.models import Evidence, SelectionSettings
from app.refresh.policy import ALLOWED_FIELDS, RELATION_KEYS


def test_policy_separates_input_and_reference_budgets():
    policy = SelectionSettings(max_documents=2)
    assert policy.max_changed_documents == 20
    assert policy.max_input_chars == 5000
    assert policy.max_documents == 2
    assert policy.max_model_calls == 20
    assert ALLOWED_FIELDS == {"body_text"}
    assert RELATION_KEYS["LOCATION"] == "related_place"


def test_evidence_uses_original_codepoint_offsets_and_is_immutable():
    body = "가\n😀나다"
    evidence = Evidence("d", 2, 2, 4, "😀나")
    assert body[evidence.start : evidence.end] == evidence.quote
    with pytest.raises(FrozenInstanceError):
        evidence.start = 0
