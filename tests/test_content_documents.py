"""기존 Content API 계약·신원·본문 변환·실패 경계를 확인한다."""

from uuid import uuid4

import httpx
import pytest

from app.adapters.content_documents import ContentDocumentSource
from app.adapters.neptune_refresh import neptune_refresh_service
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentState
from tests.fakes.content import content_payload


@pytest.fixture
def api():
    user, project, document, missing = (str(uuid4()) for _ in range(4))
    payload = content_payload(project, document)
    requests = []
    responses = {
        f"/projects/{project}": httpx.Response(
            200, json={"id": project, "trashed_at": None}
        ),
        f"/files/{document}/content": httpx.Response(200, json=payload),
        f"/files/{missing}/content": httpx.Response(
            404, json={"code": "FILE_NOT_FOUND"}
        ),
    }

    def respond(request):
        requests.append(request)
        value = responses[request.url.path]
        if isinstance(value, Exception):
            raise value
        return value

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with ContentDocumentSource(
            "http://content-api", user_id=user, client=client
        ) as adapter:
            yield adapter, (user, project, document, missing), responses, requests
        assert not client.is_closed  # 주입된 클라이언트 소유권을 유지한다.


def test_existing_api_identity_scope_missing_and_immutable_fields(api):
    adapter, (user, project, document, missing), _, requests = api
    batch = adapter.fetch_documents(project, (document, missing, document))
    assert [request.url.path for request in requests] == [
        f"/projects/{project}",
        f"/files/{document}/content",
        f"/files/{missing}/content",
    ]
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers.get_list("X-User-Id") == [user] for request in requests)
    assert batch.missing_ids == (missing,)
    (snapshot,) = batch.documents
    assert (snapshot.project_id, snapshot.document_id) == (project, document)
    assert (snapshot.title, snapshot.folder_code) == ("기존 인물", "CHARACTER")
    assert (snapshot.body_text, snapshot.revision_no) == ("Content 원문", 7)
    assert snapshot.properties == (("aliases", "별칭  "),)
    assert snapshot.state == DocumentState.ACTIVE


def test_editor_text_preserves_content_newlines_and_relation_description(api):
    adapter, (_, project, document, target), responses, _ = api
    body = {
        "schema_version": 1,
        "doc": {
            "type": "doc",
            "content": [
                {"type": "paragraph"},
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": " 앞 ", "marks": [{"type": "bold"}]},
                        {"type": "text", "text": "뒤😀"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "다음"},
                    ],
                },
                {"type": "paragraph"},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "하나"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "둘"}],
                                }
                            ],
                        },
                    ],
                },
                {
                    "type": "blockquote",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "인용"}],
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "보존"}],
                        },
                    ],
                },
            ],
        },
    }
    responses[f"/files/{document}/content"] = httpx.Response(
        200,
        json=content_payload(
            project,
            document,
            body=body,
            revision_no=0,
            relations=[
                {
                    "target_document_id": target,
                    "relation_key": "related_place",
                    "description": "연결 설명  ",
                }
            ],
        ),
    )
    (snapshot,) = adapter.fetch_documents(project, (document,)).documents
    assert snapshot.body_text == " 앞 뒤😀\n다음\n\n하나\n둘\n인용\n보존"
    assert snapshot.revision_no == 0
    (relation,) = snapshot.relations
    assert (relation.project_id, relation.document_id, relation.target_document_id) == (
        project,
        document,
        target,
    )
    assert (relation.relation_key, relation.description) == (
        "related_place",
        "연결 설명  ",
    )


@pytest.mark.parametrize("text", ["", "# 아직 변환 전\n원문  \n"])
def test_legacy_body_is_preserved_without_reinterpreting_markdown(api, text):
    adapter, (_, project, document, _), responses, _ = api
    responses[f"/files/{document}/content"] = httpx.Response(
        200,
        json=content_payload(
            project,
            document,
            body=None,
            legacy_body_md=text,
        ),
    )
    assert adapter.fetch_documents(project, (document,)).documents[0].body_text == text


@pytest.mark.parametrize(
    "change,code",
    [
        ({"project_id": str(uuid4())}, "PROJECT_MISMATCH"),
        ({"id": str(uuid4())}, "FETCH_MISMATCH"),
        ({"revision_no": True}, "CONTENT_RESPONSE_INVALID"),
        ({"revision_no": -1}, "CONTENT_RESPONSE_INVALID"),
        ({"revision_no": "7"}, "CONTENT_RESPONSE_INVALID"),
        ({"folder_code": "UNKNOWN"}, "CONTENT_RESPONSE_INVALID"),
        ({"body": None}, "CONTENT_RESPONSE_INVALID"),
        (
            {"body": {"schema_version": 2, "doc": {"type": "doc"}}},
            "CONTENT_RESPONSE_INVALID",
        ),
        (
            {
                "body": {
                    "schema_version": 1,
                    "doc": {"type": "doc", "content": [{"type": "unknown"}]},
                }
            },
            "CONTENT_RESPONSE_INVALID",
        ),
        (
            {
                "body": {
                    "schema_version": 1,
                    "doc": {"type": "doc", "content": [{"type": "text"}]},
                }
            },
            "CONTENT_RESPONSE_INVALID",
        ),
        ({"properties": [{"key": "a", "value": 5}]}, "CONTENT_RESPONSE_INVALID"),
    ],
)
def test_invalid_or_out_of_scope_response_never_becomes_context(api, change, code):
    adapter, (_, project, document, _), responses, _ = api
    responses[f"/files/{document}/content"] = httpx.Response(
        200, json=content_payload(project, document) | change
    )
    with pytest.raises(RefreshFailure) as caught:
        adapter.fetch_documents(project, (document,))
    assert caught.value.failure.code == code
    assert not caught.value.failure.retryable
    assert "Content 원문" not in str(caught.value)


@pytest.mark.parametrize(
    "status,payload,code,retryable",
    [
        (401, {}, "CONTENT_ACCESS_DENIED", False),
        (403, {}, "CONTENT_ACCESS_DENIED", False),
        (408, {}, "CONTENT_UNAVAILABLE", True),
        (429, {}, "CONTENT_UNAVAILABLE", True),
        (503, {}, "CONTENT_UNAVAILABLE", True),
        (400, {"code": "INVALID_REQUEST"}, "CONTENT_REQUEST_REJECTED", False),
        (404, {"code": "NOT_FOUND"}, "CONTENT_REQUEST_REJECTED", False),
        (302, {}, "CONTENT_REQUEST_REJECTED", False),
        (200, [], "CONTENT_RESPONSE_INVALID", False),
    ],
)
def test_http_failures_and_redirects_are_not_missing_documents(
    api, status, payload, code, retryable
):
    adapter, (_, project, document, _), responses, requests = api
    responses[f"/files/{document}/content"] = httpx.Response(
        status, json=payload, headers={"Location": "http://untrusted.test"}
    )
    with pytest.raises(RefreshFailure) as caught:
        adapter.fetch_documents(project, (document,))
    assert (caught.value.failure.code, caught.value.failure.retryable) == (
        code,
        retryable,
    )
    assert len(requests) == 2


def test_network_and_invalid_json_do_not_expose_upstream_data(api):
    adapter, (_, project, document, _), responses, _ = api
    for value, code, retryable in [
        (httpx.ReadTimeout("private upstream failure"), "CONTENT_UNAVAILABLE", True),
        (
            httpx.Response(200, content=b"private upstream failure"),
            "CONTENT_RESPONSE_INVALID",
            False,
        ),
    ]:
        responses[f"/files/{document}/content"] = value
        with pytest.raises(RefreshFailure) as caught:
            adapter.fetch_documents(project, (document,))
        assert (caught.value.failure.code, caught.value.failure.retryable) == (
            code,
            retryable,
        )
        assert "private upstream" not in str(caught.value)


@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(404, json={"code": "PROJECT_NOT_FOUND"}),
            "CONTENT_PROJECT_UNAVAILABLE",
        ),
        (
            httpx.Response(200, json={"id": str(uuid4()), "trashed_at": None}),
            "PROJECT_MISMATCH",
        ),
    ],
)
def test_unavailable_project_stops_before_body_requests(api, response, code):
    adapter, (_, project, document, _), responses, requests = api
    responses[f"/projects/{project}"] = response
    with pytest.raises(RefreshFailure) as caught:
        adapter.fetch_documents(project, (document,))
    assert caught.value.failure.code == code
    assert len(requests) == 1


def test_no_ids_means_no_io_and_invalid_ids_fail_before_io(api):
    adapter, (_, project, document, _), _, requests = api
    assert adapter.fetch_documents(project, ()).documents == ()
    with pytest.raises(RefreshFailure, match="Canonical"):
        adapter.fetch_documents(project, (document, "../other"))
    assert requests == []


def test_environment_has_only_service_config_not_user_identity(monkeypatch):
    monkeypatch.setenv("CONTENT_BASE_URL", "http://content-api")
    monkeypatch.setenv("CONTENT_TIMEOUT_SECONDS", "1.5")
    user = str(uuid4())
    with ContentDocumentSource.from_env(user_id=user) as adapter:
        assert adapter.user_id == user
        assert adapter.base_url == "http://content-api"
        assert adapter._timeout.read == 1.5
    assert adapter._client.is_closed
    with pytest.raises(ValueError, match="trusted user_id"):
        with neptune_refresh_service(
            artifacts=None, model=None, inbox=None, publisher=None
        ):
            pytest.fail("Missing identity must not produce a service")
    for value in (
        "",
        "http://user:password@content-api",
        "http://content-api?key=value",
    ):
        with pytest.raises(ValueError):
            ContentDocumentSource(value, user_id=user)
    with pytest.raises(ValueError):
        ContentDocumentSource("http://content-api", user_id="not-an-identity")
