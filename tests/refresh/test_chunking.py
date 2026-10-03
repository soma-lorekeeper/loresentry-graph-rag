from dataclasses import replace

import pytest

from app.refresh.evidence import unique_evidence
from app.refresh.models import Evidence
from app.text.chunking import chunk_spans


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \n  ",
        "같은 말. 같은 말.\n같은 말.",
        "😀가\n나다! 끝?",
        "긴문장" * 2000,
        "  첫 문장.\n\n  다음 문장.  ",
    ],
)
@pytest.mark.parametrize("size", [1, 10, 1000])
def test_chunks_preserve_every_original_codepoint(text, size):
    spans = chunk_spans(text, size)
    assert "".join(text[start:end] for start, end in spans) == text
    assert all(0 < end - start <= size for start, end in spans)
    assert all(
        left[1] == right[0] for left, right in zip(spans, spans[1:], strict=False)
    )
    assert spans == chunk_spans(text, size)


def test_evidence_dedup_keeps_revisions_and_repeated_occurrences():
    first = Evidence("doc", 1, 0, 2, "문장")
    second = replace(first, start=3, end=5)
    revised = replace(first, revision_no=2)
    assert len(unique_evidence((first, second, revised, first))) == 3


@pytest.mark.parametrize("size", [0, -1, True])
def test_invalid_chunk_budget(size):
    with pytest.raises(ValueError):
        chunk_spans("text", size)
