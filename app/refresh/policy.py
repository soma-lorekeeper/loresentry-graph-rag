"""Content 분류와 MVP의 고정 계약. 런타임 IO를 수행하지 않는다."""

RELATION_KEYS = {
    "WORLDVIEW": "related_worldview",
    "CHARACTER": "related_character",
    "LOCATION": "related_place",
    "MANUSCRIPT": "related_manuscript",
    "ORGANIZATION": "related_organization",
    "ITEM": "related_item",
    "EVENT": "related_event",
}
ALLOWED_FIELDS = frozenset({"body_text"})
MAX_CHANGED_DOCUMENTS = 20
MAX_BODY_CHARS = 5000
