import os
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = 0x01
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
MAX_PLAINTEXT_BYTES = 32 * 1024 * 1024


class EnvelopeError(ValueError):
    pass


def is_envelope(data: bytes) -> bool:
    return data[:1] == bytes((FORMAT,))


def associated_data(project_id: str, subject_id: str, purpose: str) -> bytes:
    return f"lore-sentry:v1:{project_id}:{subject_id}:{purpose}".encode()


def _nonce() -> bytes:
    return os.urandom(NONCE_BYTES)


def _cipher(key: bytes) -> AESGCM:
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise EnvelopeError("Manuscript key must be 32 bytes")
    return AESGCM(key)


def seal(
    key: bytes, project_id: str, subject_id: str, purpose: str, plaintext: bytes
) -> bytes:
    nonce = _nonce()
    ciphertext = _cipher(key).encrypt(
        nonce,
        zlib.compress(plaintext),
        associated_data(project_id, subject_id, purpose),
    )
    return bytes((FORMAT,)) + nonce + ciphertext


def open_envelope(
    key: bytes, project_id: str, subject_id: str, purpose: str, envelope: bytes
) -> bytes:
    if not is_envelope(envelope) or len(envelope) < 1 + NONCE_BYTES + TAG_BYTES:
        raise EnvelopeError("Unsupported manuscript envelope")
    try:
        compressed = _cipher(key).decrypt(
            envelope[1 : 1 + NONCE_BYTES],
            envelope[1 + NONCE_BYTES :],
            associated_data(project_id, subject_id, purpose),
        )
    except InvalidTag:
        raise EnvelopeError("Manuscript envelope authentication failed") from None
    inflater = zlib.decompressobj()
    try:
        plaintext = inflater.decompress(compressed, MAX_PLAINTEXT_BYTES + 1)
    except zlib.error:
        raise EnvelopeError("Manuscript envelope is not compressed data") from None
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise EnvelopeError("Manuscript envelope exceeds the plaintext limit")
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise EnvelopeError("Manuscript envelope has invalid compressed data")
    return plaintext
