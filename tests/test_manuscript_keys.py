from hashlib import sha256
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.adapters.manuscript_keys import ManuscriptKeys, manuscript_keys
from app.refresh.errors import RefreshFailure

PROJECT = "0b5e0c5e-6a8f-4e0e-9a5c-1f2d3c4b5a69"
OTHER = "11111111-1111-4111-8111-111111111111"
KEY_ID = "alias/lore-sentry-manuscript-test"


class FakeKms:
    def __init__(self, failure=None, plaintext=None):
        self.calls = []
        self.failure = failure
        self.plaintext = plaintext

    def decrypt(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        if self.plaintext is not None:
            return {"Plaintext": self.plaintext}
        context = kwargs["EncryptionContext"]["project_id"].encode()
        return {"Plaintext": sha256(context + kwargs["CiphertextBlob"]).digest()}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_unwrap_uses_key_id_and_project_context_and_caches_for_ten_minutes():
    kms, clock = FakeKms(), Clock()
    keys = ManuscriptKeys(kms, KEY_ID, clock=clock)
    first = keys.unwrap(PROJECT, b"wrapped")
    assert len(first) == 32
    assert kms.calls == [
        {
            "CiphertextBlob": b"wrapped",
            "KeyId": KEY_ID,
            "EncryptionContext": {"project_id": PROJECT},
        }
    ]
    clock.now = 599
    assert keys.unwrap(PROJECT, b"wrapped") == first
    assert len(kms.calls) == 1
    assert keys.unwrap(OTHER, b"wrapped") != first
    assert keys.unwrap(PROJECT, b"rotated") != first
    assert len(kms.calls) == 3
    clock.now = 600
    assert keys.unwrap(PROJECT, b"wrapped") == first
    assert len(kms.calls) == 4


def test_cache_is_bounded_by_least_recent_use():
    kms = FakeKms()
    keys = ManuscriptKeys(kms, KEY_ID, max_entries=2)
    keys.unwrap(PROJECT, b"a")
    keys.unwrap(PROJECT, b"b")
    keys.unwrap(PROJECT, b"a")
    keys.unwrap(PROJECT, b"c")
    assert len(keys._cache) == 2
    keys.unwrap(PROJECT, b"a")
    assert len(kms.calls) == 3
    keys.unwrap(PROJECT, b"b")
    assert len(kms.calls) == 4


@pytest.mark.parametrize(
    "kms",
    [
        FakeKms(
            ClientError(
                {
                    "Error": {
                        "Code": "InvalidCiphertextException",
                        "Message": "private detail",
                    }
                },
                "Decrypt",
            )
        ),
        FakeKms(ClientError({"Error": {"Code": "ThrottlingException"}}, "Decrypt")),
        FakeKms(EndpointConnectionError(endpoint_url="https://private-kms")),
        FakeKms(plaintext=b"short"),
    ],
)
def test_kms_failures_are_retryable_and_sanitized(kms):
    keys = ManuscriptKeys(kms, KEY_ID)
    with pytest.raises(RefreshFailure) as caught:
        keys.unwrap(PROJECT, b"wrapped")
    assert caught.value.failure.code == "MANUSCRIPT_KEY_UNAVAILABLE"
    assert caught.value.failure.retryable
    assert "private" not in str(caught.value)
    assert not keys._cache


@pytest.mark.parametrize(
    "kwargs", [{"ttl_seconds": 0}, {"max_entries": 0}, {"max_entries": 1.5}]
)
def test_invalid_cache_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ManuscriptKeys(FakeKms(), KEY_ID, **kwargs)
    with pytest.raises(ValueError):
        ManuscriptKeys(FakeKms(), " ")


def test_factory_is_optional_and_builds_a_regional_kms_client(monkeypatch):
    import app.adapters.manuscript_keys as module

    factory = Mock(side_effect=AssertionError("no client without a key id"))
    monkeypatch.setattr(module.boto3, "client", factory)
    with manuscript_keys({}) as keys:
        assert keys is None
    with (
        pytest.raises(ValueError, match="MANUSCRIPT_KEY_REGION"),
        manuscript_keys({"MANUSCRIPT_KEY_ID": KEY_ID}),
    ):
        pass
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(module.boto3, "client", factory)
    env = {"MANUSCRIPT_KEY_ID": KEY_ID, "MANUSCRIPT_KEY_REGION": "ap-northeast-2"}
    with manuscript_keys(env) as keys:
        assert keys.client is client and keys.key_id == KEY_ID
    assert factory.call_args.args == ("kms",)
    assert factory.call_args.kwargs["region_name"] == "ap-northeast-2"
    assert not any(k.startswith("aws_") for k in factory.call_args.kwargs)
    client.close.assert_called_once()
