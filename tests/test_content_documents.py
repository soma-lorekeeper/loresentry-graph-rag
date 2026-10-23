"""Content 목록 API 계약·신원·문서 변환·실패 경계를 확인한다."""

from uuid import uuid4

import httpx
import pytest

from app.adapters.content_documents import CONTENTS_MAX, ContentDocumentSource
from app.adapters.neptune_refresh import neptune_refresh_service
from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentState
from tests.fakes.content import content_document, contents_payload


@pytest.fixture
def api():
    user, project, document, missing = (str(uuid4()) for _ in range(4))
    requests = []
    state = {
        "response": httpx.Response(
            200,
            json=contents_payload(
                project, [content_document(document)], missing=[missing]
            ),
        )
    }

    def respond(request):
        requests.append(request)
        value = state["response"]
        if isinstance(value, Exception):
            raise value
        return value(request) if callable(value) else value

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with ContentDocumentSource(
            "http://content-api", user_id=user, client=client
        ) as adapter:
            yield adapter, (user, project, document, missing), state, requests
        assert not client.is_closed  # 주입된 클라이언트 소유권을 유지한다.


def test_list_api_identity_scope_missing_and_fields(api):
    adapter, (user, project, document, missing), _, requests = api
    batch = adapter.fetch_documents(project, (document, missing, document))
    (request,) = requests
    assert request.method == "GET"
    assert request.url.path == f"/projects/{project}/files/content"
    # 중복을 합치고 요청 순서대로 ids를 반복 파라미터로 보낸다.
    assert request.url.params.get_list("ids") == [document, missing]
    assert request.headers.get_list("X-User-Id") == [user]
    assert batch.missing_ids == (missing,)
    (snapshot,) = batch.documents
    assert (snapshot.project_id, snapshot.document_id) == (project, document)
    assert (snapshot.title, snapshot.folder_code) == ("기존 인물", "CHARACTER")
    assert (snapshot.body_text, snapshot.revision_no) == ("Content 원문", 7)
    assert snapshot.properties == (("aliases", "별칭  "),)
    assert snapshot.state == DocumentState.ACTIVE


def test_ids_are_split_by_the_content_limit(api):
    adapter, (_, project, _, _), state, requests = api
    ids = tuple(str(uuid4()) for _ in range(CONTENTS_MAX + 1))

    def echo(request):
        asked = request.url.params.get_list("ids")
        return httpx.Response(
            200,
            json=contents_payload(project, [content_document(i) for i in asked]),
        )

    state["response"] = echo
    batch = adapter.fetch_documents(project, ids)
    assert [len(r.url.params.get_list("ids")) for r in requests] == [CONTENTS_MAX, 1]
    assert tuple(d.document_id for d in batch.documents) == ids


def test_relations_and_repeated_property_keys_are_kept(api):
    adapter, (_, project, document, target), state, _ = api
    state["response"] = httpx.Response(
        200,
        json=contents_payload(
            project,
            [
                content_document(
                    document,
                    revision_no=0,
                    properties=[
                        {"key": "alias", "value": "첫째"},
                        {"key": "alias", "value": "둘째"},
                    ],
                    relations=[
                        {
                            "target_document_id": target,
                            "relation_key": "related_place",
                            "description": "연결 설명  ",
                        }
                    ],
                )
            ],
        ),
    )
    (snapshot,) = adapter.fetch_documents(project, (document,)).documents
    assert snapshot.properties == (("alias", "첫째"), ("alias", "둘째"))
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


@pytest.mark.parametrize(
    "change,code",
    [
        ({"revision_no": True}, "CONTENT_RESPONSE_INVALID"),
        ({"revision_no": -1}, "CONTENT_RESPONSE_INVALID"),
        ({"revision_no": "7"}, "CONTENT_RESPONSE_INVALID"),
        ({"folder_code": "UNKNOWN"}, "CONTENT_RESPONSE_INVALID"),
        ({"body_text": None}, "CONTENT_RESPONSE_INVALID"),
        ({"title": ""}, "CONTENT_RESPONSE_INVALID"),
        ({"properties": [{"key": "a", "value": 5}]}, "CONTENT_RESPONSE_INVALID"),
        ({"properties": [{"key": " ", "value": "a"}]}, "CONTENT_RESPONSE_INVALID"),
        (
            {
                "relations": [
                    {
                        "target_document_id": str(uuid4()),
                        "relation_key": "unknown",
                        "description": "",
                    }
                ]
            },
            "CONTENT_RESPONSE_INVALID",
        ),
        ({"id": str(uuid4())}, "FETCH_MISMATCH"),
    ],
)
def test_invalid_document_never_becomes_context(api, change, code):
    adapter, (_, project, document, _), state, _ = api
    state["response"] = httpx.Response(
        200, json=contents_payload(project, [content_document(document) | change])
    )
    with pytest.raises(RefreshFailure) as caught:
        adapter.fetch_documents(project, (document,))
    assert caught.value.failure.code == code
    assert not caught.value.failure.retryable
    assert "Content 원문" not in str(caught.value)


def test_response_must_cover_exactly_the_requested_ids(api):
    adapter, (_, project, document, missing), state, _ = api
    cases = [
        (str(uuid4()), [content_document(document)], [], "PROJECT_MISMATCH"),
        (project, [content_document(document)], [], "FETCH_MISMATCH"),
        (project, [content_document(document)], [document], "FETCH_MISMATCH"),
        (project, [content_document(document)] * 2, [missing], "FETCH_MISMATCH"),
        (project, [content_document(document)], [missing, missing], "FETCH_MISMATCH"),
    ]
    for owner, documents, absent, code in cases:
        state["response"] = httpx.Response(
            200, json=contents_payload(owner, documents, absent)
        )
        with pytest.raises(RefreshFailure) as caught:
            adapter.fetch_documents(project, (document, missing))
        assert caught.value.failure.code == code


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
        (404, {"code": "PROJECT_NOT_FOUND"}, "CONTENT_PROJECT_UNAVAILABLE", False),
        (302, {}, "CONTENT_REQUEST_REJECTED", False),
        (200, [], "CONTENT_RESPONSE_INVALID", False),
    ],
)
def test_http_failures_and_redirects_are_not_missing_documents(
    api, status, payload, code, retryable
):
    adapter, (_, project, document, _), state, requests = api
    state["response"] = httpx.Response(
        status, json=payload, headers={"Location": "http://untrusted.test"}
    )
    with pytest.raises(RefreshFailure) as caught:
        adapter.fetch_documents(project, (document,))
    assert (caught.value.failure.code, caught.value.failure.retryable) == (
        code,
        retryable,
    )
    assert len(requests) == 1


def test_network_and_invalid_json_do_not_expose_upstream_data(api):
    adapter, (_, project, document, _), state, _ = api
    for value, code, retryable in [
        (httpx.ReadTimeout("private upstream failure"), "CONTENT_UNAVAILABLE", True),
        (
            httpx.Response(200, content=b"private upstream failure"),
            "CONTENT_RESPONSE_INVALID",
            False,
        ),
    ]:
        state["response"] = value
        with pytest.raises(RefreshFailure) as caught:
            adapter.fetch_documents(project, (document,))
        assert (caught.value.failure.code, caught.value.failure.retryable) == (
            code,
            retryable,
        )
        assert "private upstream" not in str(caught.value)


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
        with neptune_refresh_service(artifacts=None, model=None, inbox=None):
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
