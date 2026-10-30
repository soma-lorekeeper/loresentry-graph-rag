import json
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import app.manuscript_crypto as crypto
from app.manuscript_crypto import (
    MAX_PLAINTEXT_BYTES,
    EnvelopeError,
    associated_data,
    is_envelope,
    open_envelope,
    seal,
)

VECTORS = json.loads(
    (Path(__file__).parent / "manuscript-envelope-vectors.json").read_text()
)
KEY = bytes.fromhex(VECTORS["key_hex"])
PROJECT = "0b5e0c5e-6a8f-4e0e-9a5c-1f2d3c4b5a69"
SUBJECT = "c3d4e5f6-0718-4293-a4b5-c6d7e8f90a1b"


def raw_envelope(compressed, purpose="refresh_result"):
    nonce = bytes(12)
    return (
        b"\x01"
        + nonce
        + AESGCM(KEY).encrypt(
            nonce, compressed, associated_data(PROJECT, SUBJECT, purpose)
        )
    )


@pytest.mark.parametrize("vector", VECTORS["vectors"])
def test_vectors_decrypt_and_seal_byte_for_byte(vector, monkeypatch):
    envelope = bytes.fromhex(vector["envelope_hex"])
    ids = (vector["project_id"], vector["subject_id"], vector["purpose"])
    assert is_envelope(envelope)
    assert open_envelope(KEY, *ids, envelope) == vector["plaintext"].encode()
    monkeypatch.setattr(crypto, "_nonce", lambda: envelope[1:13])
    assert seal(KEY, *ids, vector["plaintext"].encode()) == envelope


@pytest.mark.parametrize("vector", VECTORS["vectors"])
def test_changed_purpose_subject_project_or_key_fails(vector):
    envelope = bytes.fromhex(vector["envelope_hex"])
    project, subject, purpose = (
        vector["project_id"],
        vector["subject_id"],
        vector["purpose"],
    )
    for args in (
        (KEY, project, subject, purpose + "_other"),
        (KEY, project, PROJECT if subject != PROJECT else SUBJECT, purpose),
        (KEY, SUBJECT, subject, purpose),
        (bytes(32), project, subject, purpose),
    ):
        with pytest.raises(EnvelopeError):
            open_envelope(*args, envelope)
    tampered = envelope[:-1] + bytes((envelope[-1] ^ 1,))
    with pytest.raises(EnvelopeError):
        open_envelope(KEY, project, subject, purpose, tampered)


def test_round_trip_uses_a_fresh_nonce():
    plaintext = "유중혁 😀\n".encode() * 100
    first = seal(KEY, PROJECT, SUBJECT, "refresh_context", plaintext)
    second = seal(KEY, PROJECT, SUBJECT, "refresh_context", plaintext)
    assert first != second
    assert first[0] == 1 and len(first) < len(plaintext)
    for envelope in (first, second):
        assert (
            open_envelope(KEY, PROJECT, SUBJECT, "refresh_context", envelope)
            == plaintext
        )


def test_decompression_is_capped_at_32_mib():
    exact = b"a" * MAX_PLAINTEXT_BYTES
    envelope = seal(KEY, PROJECT, SUBJECT, "refresh_result", exact)
    assert open_envelope(KEY, PROJECT, SUBJECT, "refresh_result", envelope) == exact
    bomb = seal(KEY, PROJECT, SUBJECT, "refresh_result", exact + b"a")
    assert len(bomb) < 64 * 1024
    with pytest.raises(EnvelopeError, match="limit"):
        open_envelope(KEY, PROJECT, SUBJECT, "refresh_result", bomb)


@pytest.mark.parametrize(
    "compressed",
    [
        zlib.compress(b"{}")[:-1],
        zlib.compress(b"{}") + b"extra",
        b"not zlib",
    ],
)
def test_invalid_compressed_payload_is_rejected(compressed):
    with pytest.raises(EnvelopeError):
        open_envelope(KEY, PROJECT, SUBJECT, "refresh_result", raw_envelope(compressed))


@pytest.mark.parametrize(
    "envelope", [b"", b"{}", b"\x02" + bytes(40), b"\x01" + bytes(27)]
)
def test_unsupported_or_short_envelopes_are_rejected(envelope):
    with pytest.raises(EnvelopeError):
        open_envelope(KEY, PROJECT, SUBJECT, "refresh_result", envelope)


def test_key_must_be_32_bytes():
    for key in (bytes(16), "0" * 32, None):
        with pytest.raises(EnvelopeError):
            seal(key, PROJECT, SUBJECT, "refresh_result", b"{}")
    assert not is_envelope(b'{"schema_version":"refresh-input-v2"}')
