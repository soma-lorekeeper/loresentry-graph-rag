"""AI docstore의 출처 중복 제거 원칙을 문서·revision·원문 구간에 적용한다."""

from app.refresh.models import Chunk, DocumentState, Evidence
from app.text.chunking import chunk_spans


def unique_evidence(items):
    """내용이 같아도 다른 revision이나 위치의 근거를 합치지 않는다."""
    return tuple(
        sorted(
            set(items),
            key=lambda e: (e.document_id, e.revision_no, e.start, e.end, e.quote),
        )
    )


def make_chunks(snapshot):
    """활성 문서 전체를 원문 위치를 보존하여 분할한다."""
    return tuple(
        Chunk(
            Evidence(
                document.document_id,
                document.revision_no,
                start,
                end,
                document.body_text[start:end],
            )
        )
        for document in sorted(snapshot.documents, key=lambda d: d.document_id)
        if document.state == DocumentState.ACTIVE
        for start, end in chunk_spans(
            document.body_text, snapshot.request.settings.chunk_chars
        )
    )
