from dataclasses import FrozenInstanceError, fields

import pytest

from app.refresh.models import Evidence, SelectionSettings
from app.refresh.policy import ALLOWED_FIELDS, MAX_PROPOSAL_CHARS, RELATION_KEYS


def test_policy_limits_calls_and_chunks_but_not_input_count_or_length():
    """선택 정책에는 문서 수·본문 길이 한도가 없다. 입력 크기는 모델 토큰 예산이 본다."""
    policy = SelectionSettings()
    assert {field.name for field in fields(SelectionSettings)} == {
        "relation_keys",
        "max_depth",
        "policy_version",
        "max_model_calls",
        "chunk_chars",
        "separate_discovery",
        "chunk_overlap_chars",
    }
    assert policy.max_model_calls == 40
    # 제안 본문·관계 설명 상한은 Content 본문 최대 길이와 같다.
    assert MAX_PROPOSAL_CHARS == 1_000_000
    assert ALLOWED_FIELDS == {"body_text"}
    assert RELATION_KEYS["LOCATION"] == "related_place"


def test_evidence_uses_original_codepoint_offsets_and_is_immutable():
    body = "가\n😀나다"
    evidence = Evidence("d", 2, 2, 4, "😀나")
    assert body[evidence.start : evidence.end] == evidence.quote
    with pytest.raises(FrozenInstanceError):
        evidence.start = 0
