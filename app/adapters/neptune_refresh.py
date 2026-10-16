"""refresh의 Neptune 관계 조회와 Content HTTP 조회를 연결하는 조립 지점."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from app.adapters.content_documents import ContentDocumentSource
from app.adapters.neptune_relations import NeptuneRelatedDocumentSource
from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.refresh.ports import (
    DocumentSource,
    ProposalModel,
    RefreshArtifactStore,
    RefreshInbox,
)
from app.refresh.rules import RefreshRules
from app.refresh.service import RefreshService


@contextmanager
def neptune_refresh_service(
    *,
    artifacts: RefreshArtifactStore,
    model: ProposalModel,
    inbox: RefreshInbox | None = None,
    documents: DocumentSource | None = None,
    user_id: str | None = None,
    content_base_url: str | None = None,
    rules: RefreshRules | None = None,
    endpoint: str | None = None,
) -> Iterator[RefreshService]:
    """writer 설정을 재사용하고 종료 시 소유한 HTTP 클라이언트를 닫는다.

    documents를 생략하면 신뢰한 호출자의 user_id로 실제 Content API를 사용한다.
    사용자 식별은 환경변수나 문서 입력에서 추측하지 않는다. 나머지 포트는 주입한다.
    """
    with ExitStack() as stack:
        if documents is None:
            if user_id is None:
                raise ValueError("A trusted user_id is required for Content requests")
            documents = stack.enter_context(
                ContentDocumentSource.from_env(
                    user_id=user_id, base_url=content_base_url
                )
            )
        relations = stack.enter_context(
            NeptuneRelatedDocumentSource.from_env(endpoint=endpoint)
        )
        if inbox is None:
            inbox = stack.enter_context(NeptuneRefreshInbox.from_env(endpoint=endpoint))
        yield RefreshService(
            artifacts=artifacts,
            relations=relations,
            documents=documents,
            model=model,
            inbox=inbox,
            rules=rules,
        )
