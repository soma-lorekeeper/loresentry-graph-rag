"""스냅샷을 출처 마커가 있는 모델 문맥으로 변환한다. DB 조회는 하지 않는다."""

import json
from dataclasses import asdict
from itertools import combinations
from types import SimpleNamespace

from app.refresh.evidence import unique_evidence
from app.refresh.models import DocumentState
from app.refresh.policy import RELATION_KEYS
from app.refresh.validation import relation_in_scope, setting_name


def existing_setting_identities(snapshot, active):
    """실제 설정 문서의 제목·이름·별칭만 제공한다. 본문 언급으로 이름을 만들지 않는다.

    묶음 실행에서는 묶음 밖 설정과 앞 묶음의 새 설정도 포함해 같은 이름을 다시 만들지 않게 한다.
    """
    identities = []
    known = tuple(active.values()) + getattr(snapshot, "known_settings", ())
    for document in sorted(known, key=lambda d: d.document_id):
        if (
            document.project_id != snapshot.request.job.project_id
            or document.folder_code not in RELATION_KEYS
            or document.folder_code == "MANUSCRIPT"
        ):
            continue
        values = [document.title]
        values.extend(
            value
            for key, value in document.properties
            if key in {"name", "title", "alias"}
        )
        names = sorted(
            {value for value in values if isinstance(value, str) and value.strip()},
            key=lambda value: (setting_name(value), value),
        )
        identities.append(
            {
                "document_id": document.document_id,
                "folder_code": document.folder_code,
                "names": names,
            }
        )
    return identities


def relation_scope_context(snapshot, active, scope):
    """검증 계층과 같은 소유권 규칙으로 이 호출의 새 기존 문서쌍을 열거한다."""
    if scope is None:
        return None
    existing = {
        tuple(sorted((relation.document_id, relation.target_document_id)))
        for relation in snapshot.related.relations
        + tuple(
            relation
            for document in snapshot.documents
            for relation in document.relations
        )
    }
    pairs = [
        [left, right]
        for left, right in combinations(sorted(active), 2)
        if (left, right) not in existing
        and not (
            active[left].folder_code == "MANUSCRIPT"
            and active[right].folder_code == "MANUSCRIPT"
        )
        and relation_in_scope(
            snapshot,
            SimpleNamespace(document_id=left, target_document_id=right),
            scope,
        )
    ]
    return {
        **asdict(scope),
        "target_order": list(snapshot.target_ids),
        "allowed_existing_pairs": pairs,
        "allow_new_endpoint_relations": scope.owns_fallback,
    }


def build_context(
    snapshot,
    chunks,
    target_ids=None,
    *,
    allow_new_documents=True,
    allow_document_updates=True,
    relation_scope=None,
):
    """여러 변경 문서를 함께 제공한다. 출처를 생략해 크기를 맞추지 않는다.

    크기는 모델 호출 직전의 토큰 예산 검사가 판단한다.
    """
    targets = snapshot.target_ids if target_ids is None else target_ids
    active = {
        d.document_id: d for d in snapshot.documents if d.state == DocumentState.ACTIVE
    }
    evidence = unique_evidence(chunk.evidence for chunk in chunks)
    value = {
        "project_id": snapshot.request.job.project_id,
        "target_ids": list(targets),
        "allow_new_documents": allow_new_documents,
        "allow_document_updates": allow_document_updates,
        "relation_scope": relation_scope_context(snapshot, active, relation_scope),
        "changed_ids": list(snapshot.changed_ids),
        "model_settings": asdict(snapshot.request.model_settings),
        "existing_setting_identities": existing_setting_identities(snapshot, active),
        "documents": [
            {
                "document_id": d.document_id,
                "revision_no": d.revision_no,
                "folder_code": d.folder_code,
                "properties": dict(d.properties),
                **({"title": d.title} if d.title is not None else {}),
            }
            for d in sorted(active.values(), key=lambda d: d.document_id)
        ],
        "relations": [
            asdict(r)
            for r in sorted(
                set(
                    snapshot.related.relations
                    + tuple(r for d in snapshot.documents for r in d.relations)
                ),
                key=lambda r: (
                    r.document_id,
                    r.target_document_id,
                    r.relation_key,
                    r.description,
                ),
            )
            if r.document_id in active and r.target_document_id in active
        ],
        "evidence": [
            {"marker": f"C{i:04d}", **asdict(item)}
            for i, item in enumerate(evidence, 1)
        ],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
