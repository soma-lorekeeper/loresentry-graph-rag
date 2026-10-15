"""Neptune 읽기·쓰기가 공유하는 HTTP/1.1·설정·SigV4·응답 처리."""

import json
import math
import os
from ipaddress import ip_address
from typing import Self
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError


class NeptuneUnavailable(Exception):
    """통신·충돌·불명확한 응답. 호출 유스케이스가 재시도를 결정한다."""


class NeptuneQueryRejected(Exception):
    """호출 측이 선언한 서버 측 계약 검증 실패."""


def _endpoint(value: str) -> str:
    url = urlsplit(value)
    try:
        loopback = url.hostname == "localhost" or ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = False
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or (url.scheme != "https" and not (url.scheme == "http" and loopback))
    ):
        raise ValueError("Use an HTTPS writer endpoint (HTTP only for loopback tests)")
    # 잘못된 포트도 시작 시 검출한다.
    _ = url.port
    return value.rstrip("/")


class NeptuneGremlinClient:
    """수신 측에서 재시도한다. 어댑터 자체는 자동 재시도하지 않는다."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 10.0,
        iam_auth: bool = False,
        region: str = "ap-northeast-2",
        client: httpx.Client | None = None,
        session: boto3.Session | None = None,
    ):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Neptune timeout must be positive")
        if iam_auth and not region:
            raise ValueError("AWS region is required for IAM authentication")
        self.endpoint = _endpoint(endpoint)
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(3.0, timeout_seconds)
        )
        self._owned_client = client is None
        self._client = client if client is not None else httpx.Client(http2=False)
        self._session = session or (
            boto3.Session(region_name=region) if iam_auth else None
        )
        self._iam_auth = iam_auth
        self._region = region

    @classmethod
    def from_env(cls, *, endpoint: str | None = None) -> Self:
        host = os.environ.get("NEPTUNE_ENDPOINT", "")
        if endpoint is None and (
            not host or any(char in host for char in "/:@?#") or host != host.strip()
        ):
            raise ValueError("NEPTUNE_ENDPOINT must be a writer hostname")
        port = int(os.environ.get("NEPTUNE_PORT", "8182"))
        if not 1 <= port <= 65535:
            raise ValueError("NEPTUNE_PORT must be between 1 and 65535")
        iam = os.environ.get("NEPTUNE_IAM_AUTH", "false").lower()
        if iam not in ("true", "false"):
            raise ValueError("NEPTUNE_IAM_AUTH must be true or false")
        return cls(
            endpoint if endpoint is not None else f"https://{host}:{port}",
            timeout_seconds=float(os.environ.get("NEPTUNE_TIMEOUT_SECONDS", "10")),
            iam_auth=iam == "true",
            region=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
        )

    def execute(
        self, query: str, *, rejection_codes: tuple[str, ...] = ()
    ) -> list[object]:
        body = json.dumps({"gremlin": query}, ensure_ascii=False).encode("utf-8")
        request = self._client.build_request(
            "POST",
            f"{self.endpoint}/gremlin",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/vnd.gremlin-v3.0+json;types=false",
            },
            timeout=self._timeout,
        )
        if self._iam_auth:
            self._sign(request)
        try:
            response = self._client.send(request, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise NeptuneUnavailable("Neptune response unavailable") from exc
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise NeptuneUnavailable("Neptune returned invalid JSON") from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise NeptuneUnavailable("Neptune returned an invalid response")
        status = payload.get("status")
        if response.status_code != 200 or (
            isinstance(status, dict) and status.get("code") != 200
        ):
            message = str(payload.get("detailedMessage", payload.get("message", "")))
            if isinstance(status, dict):
                message += str(status.get("message", ""))
                message += str(status.get("attributes", ""))
            for rejection in rejection_codes:
                if rejection in message:
                    raise NeptuneQueryRejected(rejection)
            raise NeptuneUnavailable(
                f"Neptune query failed (HTTP {response.status_code})"
            )
        if not isinstance(status, dict) or type(status.get("code")) is not int:
            raise NeptuneUnavailable("Neptune success status is missing")
        result = payload.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise NeptuneUnavailable("Neptune result data must be an array")
        return data

    def _sign(self, request: httpx.Request) -> None:
        try:
            credentials = self._session.get_credentials() if self._session else None
            if credentials is None:
                raise NeptuneUnavailable("AWS credentials are unavailable")
            signed = AWSRequest(
                method=request.method,
                url=str(request.url),
                data=request.content,
                headers=dict(request.headers),
            )
            SigV4Auth(
                credentials.get_frozen_credentials(), "neptune-db", self._region
            ).add_auth(signed)
            request.headers.update(dict(signed.headers))
        except BotoCoreError as exc:
            raise NeptuneUnavailable("AWS request signing failed") from exc

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
