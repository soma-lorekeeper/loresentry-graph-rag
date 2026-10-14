"""선택한 근거 안에서 복사 차이를 복원한다. 유사도는 의미 판정 점수가 아니다."""

import re
from dataclasses import replace
from difflib import SequenceMatcher
from math import ceil, floor, isfinite

from app.refresh.errors import RefreshFailure
from app.refresh.facts import fact_description
from app.refresh.models import (
    FactEntity,
    ModelCandidate,
    NewDocumentProposal,
    RelationProposal,
    SourceAlignment,
    SourceFact,
)

MIN_SIMILARITY = 0.90
MAX_WINDOWS = 4096
BOUNDARIES = frozenset(".,;:!?…—–()[]{}\"'“”‘’")
POSTPOSITIONS = (
    "이라는",
    "라는",
    "에게",
    "에서",
    "까지",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "과",
    "와",
    "의",
    "에",
    "로",
    "도",
    "만",
)
NUMBERS = re.compile(r"\d+(?:[.,]\d+)*")
KOREAN_COUNTS = re.compile(
    r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|몇)\s*"
    r"(?:개월|시간|달|년|일|시|명|개|번|회|배)"
)
NEGATIONS = re.compile(r"없|않|못|아니|불가능|(?:^|\s)안(?=\s)")


def _compact(text):
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    return "".join(text[i] for i in positions), positions


def _details_unchanged(original, restored, protected_names):
    """높은 문자 점수와 별개로 숫자·부정·알려진 인물 이름 변경을 걸러낸다."""
    for pattern in (NUMBERS, KOREAN_COUNTS, NEGATIONS):
        if [_compact(value)[0] for value in pattern.findall(original)] != [
            _compact(value)[0] for value in pattern.findall(restored)
        ]:
            return False
    left, _ = _compact(original)
    right, _ = _compact(restored)
    return all(left.count(name) == right.count(name) for name in protected_names)


def _unique_best(matches):
    if not matches:
        return None
    best_score = max(match[1] for match in matches)
    best = [match for match in matches if match[1] == best_score]
    return best[0] if len(best) == 1 else None


def find_alignment(value, excerpt, *, protected_names=()):
    """정확 일치 뒤 0.90 이상인 최상위 원문 구간을 찾는다. 동점 위치는 보정하지 않는다."""
    if not isinstance(value, str) or not 0 < len(value) <= 200:
        return None
    if value in excerpt:
        return None
    query, _ = _compact(value)
    text, positions = _compact(excerpt)
    if not query:
        return None
    occurrences = []
    start = text.find(query)
    while start >= 0:
        left, right = positions[start], positions[start + len(query) - 1] + 1
        if right - left <= 200:
            occurrences.append((excerpt[left:right], 1.0, left, right))
        start = text.find(query, start + 1)
    if occurrences:
        return _unique_best(occurrences)

    # 단어 경계에서 비교해 끝의 부정·숫자를 잘라 점수를 올리지 않는다.
    starts = [
        index
        for index, position in enumerate(positions)
        if position == 0
        or excerpt[position - 1].isspace()
        or excerpt[position - 1] in BOUNDARIES
    ]
    ends = [
        index + 1
        for index, position in enumerate(positions)
        if position + 1 == len(excerpt)
        or excerpt[position + 1].isspace()
        or excerpt[position + 1] in BOUNDARIES
    ]
    # '길'처럼 조사가 붙은 단어의 부분 표현도 비교한다.
    ends = sorted(
        set(ends)
        | {
            end - len(particle)
            for end in ends
            for particle in POSTPOSITIONS
            if text[:end].endswith(particle)
            and end > len(particle)
            and text[end - len(particle) - 1] == query[-1]
        }
    )
    minimum = ceil(len(query) * MIN_SIMILARITY / (2 - MIN_SIMILARITY))
    maximum = floor(len(query) * (2 - MIN_SIMILARITY) / MIN_SIMILARITY)
    first_word_end = len(_compact(value.split()[0])[0])
    last_word_start = len(query) - len(_compact(value.split()[-1])[0])
    matches = []
    windows = 0
    for start in starts:
        for end in ends:
            if not minimum <= end - start <= maximum:
                continue
            windows += 1
            if windows > MAX_WINDOWS:
                return None
            matcher = SequenceMatcher(None, query, text[start:end], autojunk=False)
            if matcher.quick_ratio() < MIN_SIMILARITY:
                continue
            score = matcher.ratio()
            if score < MIN_SIMILARITY:
                continue
            blocks = matcher.get_matching_blocks()
            if not any(
                block.size and block.a < first_word_end for block in blocks
            ) or not any(
                block.size and block.a + block.size > last_word_start
                for block in blocks
            ):
                continue  # 첫·마지막 단어를 통째로 버려 얻은 점수는 사용하지 않는다.
            left, right = positions[start], positions[end - 1] + 1
            restored = excerpt[left:right]
            if len(restored) > 200:
                continue
            if restored.endswith("지") and excerpt[right:].lstrip().startswith(
                ("않", "못")
            ):
                continue  # '승인하지 않았다'의 부정 보조 용언을 잘라내지 않는다.
            matches.append((restored, score, left, right))
    best = _unique_best(matches)
    if best is None or not _details_unchanged(value, best[0], protected_names):
        return None
    return best


def align_candidate(snapshot, candidate, model_input):
    """검증된 단일 서버 마커에 한정한다. 보정 뒤에는 모든 업무 검증을 다시 수행한다."""
    if not isinstance(candidate, ModelCandidate) or not isinstance(
        candidate.facts, tuple
    ):
        return candidate
    if model_input.schema_version not in {
        "refresh-candidate-v5",
        "refresh-candidate-v6",
        "refresh-candidate-v7",
        "refresh-candidate-v8",
    }:
        return candidate
    # 순환 의존을 피하고 기존 원문 검사를 그대로 재사용한다.
    from app.refresh.evidence import make_chunks
    from app.refresh.validation import validate_evidence

    chunks = {chunk.evidence for chunk in make_chunks(snapshot)}
    protected_names = {
        _compact(value)[0]
        for document in snapshot.documents
        if document.folder_code == "CHARACTER"
        for value in [document.title]
        + [v for k, v in document.properties if k in {"name", "title", "alias"}]
        if isinstance(value, str) and len(value.strip()) >= 2
    }
    if isinstance(candidate.new_document_proposals, tuple):
        protected_names.update(
            _compact(proposal.name)[0]
            for proposal in candidate.new_document_proposals
            if isinstance(proposal, NewDocumentProposal)
            and proposal.folder_code == "CHARACTER"
            and isinstance(proposal.name, str)
            and len(proposal.name.strip()) >= 2
        )
    facts = []
    for fact in candidate.facts:
        if not isinstance(fact, SourceFact) or not isinstance(fact.statement, str):
            facts.append(fact)
            continue
        try:
            sources = validate_evidence(snapshot, fact.evidence)
        except RefreshFailure:
            facts.append(fact)
            continue
        if len(sources) != 1 or sources[0] not in chunks:
            facts.append(fact)
            continue
        source = sources[0]
        updates, records = {}, []
        statement = fact.statement
        for field in ("action", "actor", "recipient", "object"):
            entity = getattr(fact, field)
            if field != "action" and not isinstance(entity, FactEntity):
                continue
            value = entity if field == "action" else entity.surface
            match = find_alignment(
                value,
                source.quote,
                protected_names=protected_names,
            )
            if match is None:
                continue
            restored, score, start, end = match
            if field != "action":
                # surface를 복원하며 요약에 새 의미를 넣거나 별개 표현을 바꾸지 않는다.
                if statement.count(value) == 1:
                    statement = statement.replace(value, restored, 1)
            updates[field] = (
                restored if field == "action" else replace(entity, surface=restored)
            )
            records.append(
                SourceAlignment(
                    field,
                    value,
                    restored,
                    score,
                    source.start + start,
                    source.start + end,
                    fact.statement,
                )
            )
        facts.append(
            replace(
                fact,
                **updates,
                statement=statement,
                source_alignments=fact.source_alignments + tuple(records),
            )
            if records
            else fact
        )
    # 관계 설명은 원래 참조 사실의 문장과 정확히 일치했을 때만 함께 갱신한다.
    # 기존에 잘못된 설명을 정상화해 숨기지는 않는다.
    previous = {
        fact.fact_id: fact for fact in candidate.facts if isinstance(fact, SourceFact)
    }
    current = {fact.fact_id: fact for fact in facts if isinstance(fact, SourceFact)}
    relations = candidate.relation_proposals
    if isinstance(relations, tuple) and len(previous) == len(candidate.facts):
        relations = tuple(
            replace(
                proposal,
                description=fact_description(
                    [current[key] for key in proposal.fact_ids]
                ),
            )
            if isinstance(proposal, RelationProposal)
            and isinstance(proposal.fact_ids, tuple)
            and all(
                isinstance(key, str) and key in previous for key in proposal.fact_ids
            )
            and proposal.description
            == fact_description([previous[key] for key in proposal.fact_ids])
            else proposal
            for proposal in relations
        )
    return replace(candidate, facts=tuple(facts), relation_proposals=relations)


def valid_alignment_records(fact):
    """저장·복구 시 보정 기록이 복원된 필드와 원문 구간에 일치하는지 확인한다."""
    records = fact.source_alignments
    if not isinstance(records, tuple) or len(records) > 4:
        return False
    if not records:
        return True
    if len(fact.evidence) != 1:
        return False
    source = fact.evidence[0]
    fields = set()
    for record in records:
        if not isinstance(record, SourceAlignment) or record.field not in {
            "action",
            "actor",
            "recipient",
            "object",
        }:
            return False
        if record.field in fields:
            return False
        fields.add(record.field)
        if (
            type(record.start) is not int
            or type(record.end) is not int
            or not source.start <= record.start < record.end <= source.end
            or type(record.similarity) not in {int, float}
            or not isfinite(record.similarity)
            or not MIN_SIMILARITY <= record.similarity <= 1
        ):
            return False
        if any(
            not isinstance(value, str) or not value or len(value) > maximum
            for value, maximum in (
                (record.original, 200),
                (record.restored, 200),
                (record.original_statement, 500),
            )
        ):
            return False
        current = getattr(fact, record.field)
        current = current.surface if isinstance(current, FactEntity) else current
        if (
            current != record.restored
            or source.quote[record.start - source.start : record.end - source.start]
            != record.restored
        ):
            return False
    return True
