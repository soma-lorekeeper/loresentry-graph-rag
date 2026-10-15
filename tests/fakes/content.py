"""Content의 DocumentResponses.Content와 같은 조회 응답 fixture."""


def content_payload(project_id, document_id, text="Content 원문", **changes):
    return {
        "id": document_id,
        "project_id": project_id,
        "title": "기존 인물",
        "folder_code": "CHARACTER",
        "episode_id": None,
        "body": {
            "schema_version": 1,
            "doc": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": text}]}
                ],
            },
        },
        "legacy_body_md": None,
        "properties": [{"key": "aliases", "value": "별칭  "}],
        "relations": [],
        "locked": False,
        "char_count": len(text),
        "revision_no": 7,
        "updated_at": "2026-10-15T00:00:00Z",
        **changes,
    }
