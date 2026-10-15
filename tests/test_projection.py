"""검증·fake 수신·HTTP 실패 경계. 그래프 동작 자체는 Gremlin 통합 테스트에서 확인."""

import json
from uuid import uuid4

import boto3
import httpx
import pytest
from pydantic import ValidationError

from app.adapters.neptune_graph import NeptuneProjectionStore
from app.projection.fake import FakeConsumer, FakeRecord
from app.projection.models import (
    ApplyResult,
    FileState,
    ProjectionChange,
    ProjectionRejected,
    ProjectionUnavailable,
    RelationState,
)
from app.projection.service import GraphProjectionHandler
from scripts.consume_graph_fixture import main


def change(**kwargs):
    return ProjectionChange(
        **{"event_id": str(uuid4()), "project_id": str(uuid4()), **kwargs}
    )


class StubStore:
    def __init__(self):
        self.calls = []
        self.fail_id = None

    def apply(self, event):
        self.calls.append(event.event_id)
        if event.event_id == self.fail_id:
            raise ProjectionUnavailable("injected transport failure")
        return ApplyResult.APPLIED


def test_failed_partition_cannot_apply_or_ack_later_records_and_can_resume():
    first = change()
    later = change(project_id=first.project_id)
    other = change()
    records = (
        FakeRecord(first.project_id, first, offset=0),
        FakeRecord(first.project_id, later, offset=1),
        FakeRecord(other.project_id, other, partition=1, offset=0),
    )
    store = StubStore()
    store.fail_id = first.event_id
    consumer = FakeConsumer(GraphProjectionHandler(store), records)
    consumer.run()
    assert store.calls == [first.event_id, other.event_id]
    assert consumer.committed_offsets == {1: 1}
    assert set(consumer.failures) == {0}

    store.fail_id = None
    consumer.resume(0)
    consumer.run()
    assert store.calls == [
        first.event_id,
        other.event_id,
        first.event_id,
        later.event_id,
    ]
    assert consumer.committed_offsets == {0: 2, 1: 1}
    consumer.run()
    assert len(store.calls) == 4


@pytest.mark.parametrize("key", ["invalid", str(uuid4()), None])
def test_handler_rejects_bad_or_foreign_key_before_io(key):
    store = StubStore()
    with pytest.raises(ProjectionRejected):
        GraphProjectionHandler(store).handle(change(), key=key)
    assert store.calls == []


def test_internal_models_reject_ambiguous_mutations():
    low, high = sorted((str(uuid4()), str(uuid4())))
    relation = RelationState(
        id=str(uuid4()), low_document_id=low, high_document_id=high
    )
    with pytest.raises(ValidationError):
        RelationState(id=str(uuid4()), low_document_id=high, high_document_id=low)
    with pytest.raises(ValidationError):
        change(relations=(relation, relation.model_copy(update={"id": str(uuid4())})))
    with pytest.raises(ValidationError):
        change(relations=(relation,), deleted_file_ids=(low,))
    with pytest.raises(ValidationError):
        change(event_type="ProjectDeleted", deleted_file_ids=(low,))
    with pytest.raises(ValidationError):
        FileState(id=low, title="x", folder_code="CHARACTER", trashed="false")
    with pytest.raises(ValidationError):
        change(source="/auth")


def test_payload_fingerprint_is_stable_for_defaults_and_changes_with_content():
    event = change()
    assert (
        event.payload_sha256
        == ProjectionChange.model_validate_json(event.model_dump_json()).payload_sha256
    )
    assert (
        event.payload_sha256
        != event.model_copy(update={"event_type": "ProjectDeleted"}).payload_sha256
    )


def response(data="APPLIED"):
    return {"status": {"code": 200}, "result": {"data": [data]}}


@pytest.mark.parametrize("result", ["APPLIED", "DUPLICATE"])
def test_one_http_request_confirms_one_result(result):
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/gremlin"
        assert request.headers["accept"].endswith("types=false")
        assert set(json.loads(request.content)) == {"gremlin"}
        return httpx.Response(200, json=response(result))

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        with NeptuneProjectionStore(
            "https://neptune.test:8182", client=client
        ) as store:
            event = change()
            assert store.apply(event) == ApplyResult(result)
        assert not client.is_closed
    assert len(requests) == 1


@pytest.mark.parametrize(
    "upstream_response",
    [
        httpx.Response(503, json={"code": "ServiceUnavailableException"}),
        httpx.Response(200, content=b"not JSON"),
        httpx.Response(200, json={}),
        httpx.Response(200, json=response("UNKNOWN")),
        httpx.Response(200, json={"status": {"code": 500}, "result": {"data": []}}),
        httpx.Response(200, json={"status": {"code": 200}, "result": {"data": []}}),
    ],
)
def test_unconfirmed_response_stops_without_ack(upstream_response):
    requests = []

    def upstream(request):
        requests.append(request)
        return upstream_response

    event = change()
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        store = NeptuneProjectionStore("https://neptune.test:8182", client=client)
        consumer = FakeConsumer(
            GraphProjectionHandler(store), (FakeRecord(event.project_id, event),)
        )
        consumer.run()
    assert len(requests) == 1
    assert consumer.committed_offsets == {}
    assert isinstance(consumer.failures[0], ProjectionUnavailable)


def test_timeout_is_not_acknowledged_or_automatically_retried():
    calls = []

    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("commit response lost", request=request)

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        store = NeptuneProjectionStore("https://neptune.test:8182", client=client)
        with pytest.raises(ProjectionUnavailable):
            store.apply(change())
    assert len(calls) == 1


def test_query_contract_failure_is_rejected_without_exposing_payload():
    def upstream(request):
        return httpx.Response(
            500,
            json={
                "code": "FailureByQueryException",
                "detailedMessage": "PROJECTION_INBOX_CONFLICT private manuscript",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        store = NeptuneProjectionStore("https://neptune.test:8182", client=client)
        with pytest.raises(ProjectionRejected, match="^PROJECTION_INBOX_CONFLICT$"):
            store.apply(change())


def test_iam_requests_are_signed_with_neptune_db_service_and_session_token():
    requests = []
    session = boto3.Session(
        aws_access_key_id="unit-access",
        aws_secret_access_key="unit-secret",
        aws_session_token="unit-session-token",
    )

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        store = NeptuneProjectionStore(
            "https://neptune.test:8182", iam_auth=True, client=client, session=session
        )
        store.apply(change())
    assert (
        "/ap-northeast-2/neptune-db/aws4_request"
        in requests[0].headers["authorization"]
    )
    assert requests[0].headers["x-amz-security-token"] == "unit-session-token"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://neptune.test:8182",
        "https://user:secret@neptune.test:8182",
        "https://neptune.test:8182/gremlin",
        "https://neptune.test:8182?x=1",
    ],
)
def test_endpoint_validation(endpoint):
    with pytest.raises(ValueError):
        NeptuneProjectionStore(endpoint)


def test_config_requires_explicit_writer_and_parses_iam(monkeypatch):
    monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="writer hostname"):
        NeptuneProjectionStore.from_env()
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "writer.neptune.test")
    monkeypatch.setenv("NEPTUNE_IAM_AUTH", "bad")
    with pytest.raises(ValueError, match="true or false"):
        NeptuneProjectionStore.from_env()


def test_cli_dry_run_uses_no_neptune(monkeypatch, tmp_path, capsys):
    event = change()
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            [{"key": event.project_id, "offset": 0, "change": event.model_dump()}]
        )
    )
    monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
    assert main([str(path), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"validated_records": 1, "writes": 0}
