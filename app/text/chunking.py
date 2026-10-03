"""AI splitters.py의 구간 기반 묶기를 이식한다. 공백·반복 문장을 생략하지 않는다.

KSS의 언어학적 문장 분류 대신 문장부호와 줄바꿈을 사용한다. 청크는 원문 전체를
겹침 없이 덮으며 항상 원문의 [start:end]이다. 검색 인덱스나 Neo4j 타입은 사용하지 않는다.
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


def chunk_spans(text: str, size: int) -> tuple[tuple[int, int], ...]:
    """최대 size 코드 포인트의 연속 구간을 반환한다. 빈 본문은 빈 결과다."""
    if type(size) is not int or size <= 0:
        raise ValueError("chunk size must be positive")
    spans = []
    for sentence in _sentence_spans(text):
        for start, end in _split_oversized_span(sentence, size):
            if spans and end - spans[-1][0] <= size:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
    return tuple(spans)
