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
# 제안 본문·관계 설명의 상한. 승인되면 Content 본문이 되므로 Content가 저장할 수 있는
# 최대 길이(DocumentService.BODY_MAX)와 같다. 입력 쪽 한도는 모델 호출의 토큰 예산이다.
MAX_PROPOSAL_CHARS = 1_000_000
