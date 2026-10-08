import io
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.response import StreamingBody

from app.adapters.refresh_artifacts import (
    S3ArtifactSettings,
    S3RefreshArtifacts,
    s3_artifacts,
)
from app.adapters.refresh_schema import fingerprint, input_bytes
from app.refresh.errors import RefreshFailure
from evaluation.cases import new_setting_cases


def setup_input(body=None, limit=8 * 1024 * 1024):
    case = new_setting_cases("gpt-5.6-luna", "s3-test")[0]
    body = input_bytes(case.source) if body is None else body
    request = replace(
        case.request,
        job=replace(case.request.job, input_fingerprint=fingerprint(body)),
        input_ref=replace(
            case.request.input_ref, bucket="input-bucket", key="refresh/input.json"
        ),
    )
    stream = StreamingBody(io.BytesIO(body), len(body))
    stream.close = Mock(wraps=stream.close)
    client = Mock()
    client.get_object.side_effect = [
        ClientError(
            {
                "Error": {"Code": "NoSuchKey"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "GetObject",
        ),
        {"Body": stream, "ContentLength": len(body)},
    ]
    store = S3RefreshArtifacts(
        client,
        S3ArtifactSettings("artifact-bucket", "input-bucket", max_object_bytes=limit),
    )
    return request, store, stream


@pytest.mark.parametrize(
    "mutation", ["extra", "revision", "identity", "body", "syntax"]
)
def test_bad_input_schema_fails_without_leaking_original_text(mutation):
    case = new_setting_cases("gpt-5.6-luna", "s3-test")[0]
    value = json.loads(input_bytes(case.source))
    if mutation == "extra":
        value["documents"][0]["extra"] = "private manuscript"
    elif mutation == "revision":
        value["documents"][0]["revision_no"] = "3"
    elif mutation == "identity":
        value["project_id"] = "another-project"
    elif mutation == "body":
        value["documents"][0]["body_text"] = None
    body = b"{invalid" if mutation == "syntax" else json.dumps(value).encode()
    request, store, stream = setup_input(body)
    with pytest.raises(RefreshFailure) as caught:
        store.read_input(request)
    assert "private manuscript" not in str(caught.value)
    assert "input-bucket" not in str(caught.value)
    stream.close.assert_called_once()


def test_object_budget_and_stream_close():
    request, store, stream = setup_input(limit=100)
    with pytest.raises(RefreshFailure) as caught:
        store.read_input(request)
    assert caught.value.failure.code == "S3_OBJECT_TOO_LARGE"
    stream.close.assert_called_once()
    # A server's understated ContentLength must not bypass the actual byte budget.
    request, store, stream = setup_input(limit=100)
    store.client.get_object.side_effect = None
    store.client.get_object.return_value = {"Body": stream, "ContentLength": 1}
    with pytest.raises(RefreshFailure) as caught:
        store._read(request.input_ref, protected=False)
    assert caught.value.failure.code == "S3_OBJECT_TOO_LARGE"
    stream.close.assert_called_once()


def test_stream_interruption_is_retryable_and_closed():
    request, store, stream = setup_input()
    stream.read = Mock(side_effect=ReadTimeoutError(endpoint_url="private endpoint"))
    with pytest.raises(RefreshFailure) as caught:
        store.read_input(request)
    assert caught.value.failure.retryable
    assert "private endpoint" not in str(caught.value)
    stream.close.assert_called_once()


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"bucket": ""},
        {"input_prefix": "../input"},
        {"prefix": "refresh/"},
        {"max_object_bytes": True},
    ],
)
def test_config_is_explicit_and_validated(values):
    if not values:
        with pytest.raises(ValueError):
            S3ArtifactSettings.from_environment({})
    else:
        settings = {
            "bucket": "artifact-bucket",
            "input_bucket": "input-bucket",
            **values,
        }
        with pytest.raises(ValueError):
            S3ArtifactSettings(**settings)


def test_factory_uses_standard_credentials_no_retries_and_closes_client(monkeypatch):
    import app.adapters.refresh_artifacts as module

    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(module.boto3, "client", factory)
    settings = S3ArtifactSettings("artifact-bucket", "input-bucket")
    with (
        pytest.raises(RuntimeError),
        s3_artifacts(settings, region_name="ap-northeast-2") as store,
    ):
        assert store.client is client
        raise RuntimeError("test failure")
    client.close.assert_called_once()
    assert factory.call_args.kwargs["config"].retries["total_max_attempts"] == 1
    assert not any(k.startswith("aws_") for k in factory.call_args.kwargs)
