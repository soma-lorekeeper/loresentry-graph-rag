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


@pytest.mark.parametrize("overlap", [-1, 10, 11, True, False, 1.5, "2", None])
def test_invalid_overlap_budget_is_rejected_even_for_empty_text(overlap):
    with pytest.raises(ValueError):
        chunk_spans("", 10, overlap)


def test_overlap_keeps_antecedent_and_action_together_at_a_sentence_boundary():
    text = "앞 문장. 유진이 왔다. 그는 편지를 받았다."
    spans = chunk_spans(text, 20, len("유진이 왔다. "))
    assert spans == ((0, text.index("그는")), (text.index("유진"), len(text)))
    assert text[spans[1][0] : spans[1][1]] == "유진이 왔다. 그는 편지를 받았다."


def test_overlap_repeats_only_whole_sentences_and_reserves_space_for_new_text():
    text = "가. 나. 다. 라. 마. 바."
    spans = chunk_spans(text, 10, 6)
    assert spans == ((0, 9), (3, 12), (6, 15), (9, 17))
    assert [text[start:end] for start, end in spans] == [
        "가. 나. 다. ",
        "나. 다. 라. ",
        "다. 라. 마. ",
        "라. 마. 바.",
    ]


def test_overlap_is_omitted_when_previous_sentence_or_next_piece_cannot_fit():
    assert chunk_spans("길고긴문장. 끝.", 8, 3) == ((0, 7), (7, 9))
    text = "앞. " + "가" * 19
    assert chunk_spans(text, 10, 3) == ((0, 3), (3, 13), (13, 22))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  \n  ",
        "같은 말. 같은 말.\n같은 말.",
        "😀가\n나다! 끝? 새 문장. ",
        "  첫 문장.\n\n  다음 문장.  ",
        "긴문장" * 2000 + ". 끝.",
    ],
)
@pytest.mark.parametrize("size", [1, 10, 40])
def test_overlapping_chunks_cover_original_unicode_and_make_bounded_progress(
    text, size
):
    overlap = size // 2
    spans = chunk_spans(text, size, overlap)
    assert spans == chunk_spans(text, size, overlap)
    assert chunk_spans(text, size, 0) == chunk_spans(text, size)
    if not text:
        assert spans == ()
        return
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(
        0 <= start < end <= len(text) and end - start <= size for start, end in spans
    )
    restored = text[spans[0][0] : spans[0][1]]
    for previous, current in zip(spans, spans[1:], strict=False):
        assert previous[0] < current[0] <= previous[1] < current[1]
        assert previous[1] - current[0] <= overlap
        restored += text[previous[1] : current[1]]
    assert restored == text
