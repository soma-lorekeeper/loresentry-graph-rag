from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from threading import Lock
from time import monotonic

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.manuscript_crypto import KEY_BYTES
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure


def unavailable(message):
    raise RefreshFailure(Failure("MANUSCRIPT_KEY_UNAVAILABLE", message, True)) from None


class ManuscriptKeys:
    def __init__(
        self,
        client,
        key_id: str,
        *,
        ttl_seconds: float = 600,
        max_entries: int = 1000,
        clock: Callable[[], float] = monotonic,
    ):
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("MANUSCRIPT_KEY_ID is required")
        if not ttl_seconds > 0 or type(max_entries) is not int or max_entries <= 0:
            raise ValueError("Manuscript key cache limits must be positive")
        self.client, self.key_id = client, key_id
        self._ttl, self._max, self._clock = ttl_seconds, max_entries, clock
        self._cache: OrderedDict[tuple[str, bytes], tuple[float, bytes]] = OrderedDict()
        self._lock = Lock()

    def unwrap(self, project_id: str, wrapped_key: bytes) -> bytes:
        entry = (project_id, bytes(wrapped_key))
        with self._lock:
            cached = self._cache.get(entry)
            if cached is not None:
                if cached[0] > self._clock():
                    self._cache.move_to_end(entry)
                    return cached[1]
                del self._cache[entry]
        try:
            response = self.client.decrypt(
                CiphertextBlob=entry[1],
                KeyId=self.key_id,
                EncryptionContext={"project_id": project_id},
            )
        except (ClientError, BotoCoreError):
            unavailable("Manuscript key could not be unwrapped")
        key = response.get("Plaintext")
        if not isinstance(key, bytes) or len(key) != KEY_BYTES:
            unavailable("Manuscript key has an unexpected length")
        with self._lock:
            self._cache[entry] = (self._clock() + self._ttl, key)
            self._cache.move_to_end(entry)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return key


@contextmanager
def manuscript_keys(values: Mapping[str, str]):
    key_id = values.get("MANUSCRIPT_KEY_ID", "").strip()
    if not key_id:
        yield None
        return
    region = values.get("MANUSCRIPT_KEY_REGION", "").strip()
    if not region:
        raise ValueError("MANUSCRIPT_KEY_REGION is required with MANUSCRIPT_KEY_ID")
    client = boto3.client(
        "kms",
        region_name=region,
        config=Config(
            retries={"mode": "standard", "total_max_attempts": 3},
            connect_timeout=5,
            read_timeout=10,
        ),
    )
    try:
        yield ManuscriptKeys(client, key_id)
    finally:
        client.close()
