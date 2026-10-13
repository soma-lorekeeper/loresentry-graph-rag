"""AI splitters.py의 구간 기반 묶기를 이식한다. 공백·반복 문장을 생략하지 않는다.

KSS의 언어학적 문장 분류 대신 문장부호와 줄바꿈을 사용한다. 청크는 원문 전체를
덮으며 항상 원문의 [start:end]이다. 기본값은 겹침이 없고 선택적으로 이전 완전한
문장을 겹친다. 검색 인덱스나 Neo4j 타입은 사용하지 않는다.
"""

import re


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    ends = [match.end() for match in re.finditer(r"[.!?。！？](?:\s+|$)|\n", text)]
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    return [
        (start, end)
        for start, end in zip([0] + ends, ends, strict=False)
        if start < end
    ]


def _split_oversized_span(span: tuple[int, int], size: int) -> list[tuple[int, int]]:
    start, end = span
    return [(index, min(index + size, end)) for index in range(start, end, size)]


def chunk_spans(
    text: str, size: int, overlap_chars: int = 0
) -> tuple[tuple[int, int], ...]:
    """크기 상한 안에서 원문을 덮는다. 겹침은 이전 청크의 완전한 문장만 허용한다."""
    if type(size) is not int or size <= 0:
        raise ValueError("chunk size must be positive")
    if type(overlap_chars) is not int or not 0 <= overlap_chars < size:
        raise ValueError("chunk overlap must be nonnegative and less than size")
    sentences = _sentence_spans(text)
    sentence_starts = {end: start for start, end in sentences}
    spans = []
    for sentence in sentences:
        for start, end in _split_oversized_span(sentence, size):
            if spans and end - spans[-1][0] <= size:
                spans[-1] = (spans[-1][0], end)
            else:
                if spans and overlap_chars:
                    previous_start, previous_end = spans[-1]
                    cursor = previous_end
                    while cursor in sentence_starts:
                        earlier = sentence_starts[cursor]
                        if (
                            earlier < previous_start
                            or previous_end - earlier > overlap_chars
                            or end - earlier > size
                        ):
                            break
                        start = cursor = earlier
                spans.append((start, end))
    return tuple(spans)
