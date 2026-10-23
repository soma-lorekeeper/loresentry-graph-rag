"""토큰 예산을 넘는 요청을 변경 문서(seed) 묶음으로 나눠 차례로 실행할 때 쓰는 순수 규칙.

묶음은 남은 seed의 앞부분과 그 seed들의 관계 이웃으로 만든다. 앞 묶음이 수락한 설정 본문
수정·새 설정·관계는 다음 묶음의 현재 상태가 되고(순차 누적), 마지막에 원래 revision 대비 누적
결과를 한 번에 검증한다. IO는 하지 않는다.
"""

from dataclasses import dataclass, replace

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import unique_evidence
from app.refresh.models import (
    DocumentSnapshot,
    ExecutionSnapshot,
    Failure,
    ModelCandidate,
    Relation,
    Usage,
)
from app.refresh.repair import failed_result
from app.refresh.validation import validate_candidate


@dataclass(frozen=True)
class BatchSnapshot(ExecutionSnapshot):
    """묶음 하나의 실행 스냅샷. 저장하지 않고 전체 스냅샷과 누적 상태에서 다시 만든다.

    Attributes:
        working_bodies: 앞 묶음이 바꾼 seed 문서의 현재 본문. seed의 body_text는 근거 위치가
            원래 revision을 가리키도록 원문 그대로 두고, 문단·조립·변경 없음 판단만 이 값을 쓴다.
        known_settings: 묶음에 없는 설정과 앞 묶음의 새 설정. 이름 중복을 막는 데만 쓴다.
    """

    working_bodies: tuple[tuple[str, str], ...] = ()
    known_settings: tuple[DocumentSnapshot, ...] = ()


class BatchState:
    """앞 묶음까지 수락한 제안·사실·시도를 누적한다. 새 설정은 후보 ID로 이어 쓴다."""

    def __init__(self):
        self.documents = {}  # 기존 설정 ID -> 최신 본문을 담은 DocumentProposal
        self.new_documents = {}  # 새 설정 후보 ID -> 최신 본문을 담은 NewDocumentProposal
        self.relations = {}  # 정렬한 문서 쌍 -> RelationProposal
        self.facts = {}  # 사실 ID -> SourceFact
        self.attempts = []
        self.usage = Usage()
        self.calls = (
            0  # 지금까지 쓴 마지막 call_index. 다음 묶음 시도 번호의 시작점이다.
        )


def _pseudo_documents(full, state):
    """앞 묶음의 새 설정 후보를 다음 묶음이 고칠 수 있는 revision 0 문서로 보여 준다."""
    project = full.request.job.project_id
    return {
        candidate_id: DocumentSnapshot(
            project,
            candidate_id,
            0,
            proposal.body_text,
            folder_code=proposal.folder_code,
            title=proposal.name,
        )
        for candidate_id, proposal in state.new_documents.items()
    }


def _accepted_relations(full, state):
    project = full.request.job.project_id
    return tuple(
        Relation(
            project,
            proposal.document_id,
            proposal.target_document_id,
            proposal.relation_key,
            proposal.description,
        )
        for proposal in state.relations.values()
    )


def batch_snapshot(full, state, seeds):
    """seed와 그 이웃(전체 선택과 같은 깊이·키 규칙)으로 묶음 스냅샷을 만든다.

    이웃은 입력에 내장된 관계, 그래프 관계, 앞 묶음이 수락한 관계로 찾는다. seed가 아닌 문서의
    본문은 앞 묶음이 바꾼 값으로 바꾸고, seed는 원문을 유지한 채 working_bodies에 현재 본문을 둔다.
    """
    documents = {d.document_id: d for d in full.documents}
    pseudo = _pseudo_documents(full, state)
    accepted = _accepted_relations(full, state)
    relations = tuple(r for d in full.documents for r in d.relations)
    relations += full.related.relations + accepted
    keys = full.request.settings.relation_keys
    frontier, members = set(seeds), set(seeds)
    for _ in range(full.request.settings.max_depth):
        following = set()
        for relation in relations:
            if keys and relation.relation_key not in keys:
                continue
            if relation.document_id in frontier:
                following.add(relation.target_document_id)
            if relation.target_document_id in frontier:
                following.add(relation.document_id)
        following -= members
        members |= following
        frontier = following
    # 요청이 직접 지정한 갱신 대상은 전체 선택처럼 모든 묶음에 넣는다.
    members |= set(full.request.target_ids)
    members &= set(documents) | set(pseudo)
    seed_set = set(seeds)
    working = {key: proposal.value for key, proposal in state.documents.items()}
    included = []
    for document in full.documents:
        if document.document_id not in members:
            continue
        if document.document_id in working and document.document_id not in seed_set:
            document = replace(document, body_text=working[document.document_id])
        included.append(document)
    new_ids = tuple(key for key in sorted(pseudo) if key in members)
    included.extend(pseudo[key] for key in new_ids)
    ids = {document.document_id for document in included}
    return BatchSnapshot(
        full.request,
        tuple(included),
        replace(
            full.related,
            document_ids=tuple(sorted(ids - seed_set)),
            relations=tuple(
                r
                for r in full.related.relations + accepted
                if r.document_id in ids and r.target_document_id in ids
            ),
        ),
        target_ids=tuple(t for t in full.target_ids if t in ids) + new_ids,
        changed_ids=tuple(seeds),
        working_bodies=tuple(
            sorted((key, working[key]) for key in seed_set if key in working)
        ),
        known_settings=tuple(
            document
            for document in tuple(full.documents) + tuple(pseudo.values())
            if document.document_id not in ids and document.folder_code != "MANUSCRIPT"
        ),
    )


def fold(state, result):
    """묶음의 검증된 결과를 누적한다. 같은 사실 ID에 다른 내용이 오면 요청을 실패시킨다."""
    created = set(state.new_documents)
    for proposal in result.new_document_proposals:
        state.new_documents[proposal.candidate_id] = proposal
    for proposal in result.document_proposals:
        key = proposal.target_document_id
        if key in created:
            # 앞 묶음의 새 설정을 고친 제안은 그 새 설정 후보의 본문으로 합친다.
            previous = state.new_documents[key]
            state.new_documents[key] = replace(
                previous,
                body_text=proposal.value,
                evidence=unique_evidence(previous.evidence + proposal.evidence),
                fact_ids=tuple(dict.fromkeys(previous.fact_ids + proposal.fact_ids)),
            )
            continue
        previous = state.documents.get(key)
        if previous is not None:
            proposal = replace(
                proposal,
                evidence=unique_evidence(previous.evidence + proposal.evidence),
                fact_ids=tuple(dict.fromkeys(previous.fact_ids + proposal.fact_ids)),
            )
        state.documents[key] = proposal
    for proposal in result.relation_proposals:
        pair = tuple(sorted((proposal.document_id, proposal.target_document_id)))
        state.relations[pair] = proposal
    for fact in result.facts:
        previous = state.facts.get(fact.fact_id)
        if previous is not None and previous != fact:
            raise RefreshFailure(
                Failure("INVALID_FACT", "Conflicting aggregate fact identifier")
            )
        state.facts[fact.fact_id] = fact
    _add_attempts(state, result)


def _add_attempts(state, result):
    """묶음의 시도 번호를 앞 묶음 뒤로 이어 붙이고 사용량을 합친다."""
    attempts = [
        replace(attempt, call_index=attempt.call_index + state.calls)
        for attempt in result.attempts
    ]
    state.attempts.extend(attempts)
    state.calls = max((a.call_index for a in attempts), default=state.calls)
    state.usage = Usage(
        state.usage.calls + result.usage.calls,
        state.usage.input_tokens + result.usage.input_tokens,
        state.usage.output_tokens + result.usage.output_tokens,
    )


def failed(state, result, failure=None):
    """한 묶음의 실패로 요청 전체를 FAILED로 끝낸다. 앞 묶음의 시도·사용량도 남긴다.

    failure를 주면 성공한 묶음 결과를 누적하다 생긴 실패(사실 충돌 등)로 바꾼다.
    """
    _add_attempts(state, result)
    return replace(
        failed_result(result, failure or result.failure, state.usage, state.attempts),
        diagnostics=result.diagnostics if failure is None else (),
        change_plan=None,
        source_scope_extensions=(),
        paragraph_copy_removals=(),
    )


def aggregate(full, state, base):
    """누적 제안을 원래 revision 기준의 후보 하나로 만들어 전체 스냅샷으로 다시 검증한다.

    새 설정 후보를 끝점으로 한 관계는 묶음에서 revision 0으로 보였으므로 미확정(None)으로 되돌린다.
    """
    created = set(state.new_documents)
    relations = tuple(
        replace(
            proposal,
            base_revision_no=None
            if proposal.document_id in created
            else proposal.base_revision_no,
            target_base_revision_no=None
            if proposal.target_document_id in created
            else proposal.target_base_revision_no,
        )
        for proposal in state.relations.values()
    )
    candidate = ModelCandidate(
        tuple(state.documents.values()),
        state.usage,
        relations,
        tuple(state.new_documents.values()),
        tuple(state.facts[key] for key in sorted(state.facts)),
    )
    # relation_scope가 None이면 묶음별 소유권·계획 조립 대신 전체 집계 규칙으로 검증한다.
    result = validate_candidate(
        full,
        candidate,
        replace(
            base,
            target_ids=full.target_ids,
            relation_scope=None,
            allow_document_updates=True,
        ),
    )
    return replace(result, usage=state.usage, attempts=tuple(state.attempts))
