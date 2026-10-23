"""Content 목록 API(GET /projects/{id}/files/content)와 같은 응답 fixture."""


def content_document(document_id, text="Content 원문", **changes):
    """documents 원소 하나. S3 refresh-input-v2 문서와 모양이 같다."""
    return {
        "id": document_id,
        "revision_no": 7,
        "folder_code": "CHARACTER",
        "title": "기존 인물",
        "body_text": text,
        "properties": [{"key": "aliases", "value": "별칭  "}],
        "relations": [],
        **changes,
    }


def contents_payload(project_id, documents=(), missing=()):
    return {
        "project_id": project_id,
        "documents": list(documents),
        "missing": list(missing),
    }
