"""refresh 완료 Inbox의 요청 식별·완료 값 검사. 외부 IO는 수행하지 않는다."""

import json
from dataclasses import asdict
from hashlib import sha256

from app.refresh.errors import RequestConflict
from app.refresh.models import Completion, RefreshRequest

CONSUMER_NAME = "graph-rag-refresh"


def request_digest(request: RefreshRequest) -> str:
    """실행마다 달라지는 값 없이 입력·설정 전체를 비교한다."""
    body = json.dumps(
        asdict(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(body).hexdigest()


def validate_completion(request: RefreshRequest, completion: Completion) -> None:
    if completion.job != request.job or completion.result_ref is None:
        raise RequestConflict("Inbox completion differs from request or has no result")
