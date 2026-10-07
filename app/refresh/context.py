"""스냅샷을 출처 마커가 있는 모델 문맥으로 변환한다. DB 조회는 하지 않는다."""

import json
from dataclasses import asdict

from app.refresh.evidence import unique_evidence
from app.refresh.models import DocumentState
from app.refresh.selection import reject


def build_context(snapshot, chunks, target_ids=None, *, allow_new_documents=True):
    """여러 변경 문서를 함께 제공한다. 출처를 생략해 문맥 예산을 맞추지 않는다."""
    targets = snapshot.target_ids if target_ids is None else target_ids
    active = {
        d.document_id: d for d in snapshot.documents if d.state == DocumentState.ACTIVE
    }
    evidence = unique_evidence(chunk.evidence for chunk in chunks)
    value = {
        "project_id": snapshot.request.job.project_id,
        "target_ids": list(targets),
        "allow_new_documents": allow_new_documents,
        "changed_ids": list(snapshot.changed_ids),
        "model_settings": asdict(snapshot.request.model_settings),
        "documents": [
            {
                "document_id": d.document_id,
                "revision_no": d.revision_no,
                "folder_code": d.folder_code,
                "properties": dict(d.properties),
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
        "max_context_chars": snapshot.request.settings.max_context_chars,
    }
    context = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(context) > snapshot.request.settings.max_context_chars:
        reject("CONTEXT_BUDGET_EXCEEDED", "Context exceeds codepoint budget")
    return context
