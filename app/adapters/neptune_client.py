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

from app.logs import AppLogger, LogField, skip_entry_log


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
    """수신 측에서 재시도한다. 어댑터 자체는 자동 재시도하지 않는다.

    Neptune은 데이터베이스라 쿼리마다 결과 줄을 남기지 않는다. 그 쿼리를 부른 repository 메서드의 진입 줄이 시작이고,
    실패만 `neptune query failed`(warn, error.type, 상대가 답했으면 응답 상태)로 남긴다. 쿼리 원문·응답 메시지와
    주소는 남기지 않는다(쿼리 리터럴에 문서 제목·설명이 들어간다).

    쿼리마다 EDOT distro의 httpx 계측이 CLIENT span(`POST`, url.full은 /gremlin까지)을 열고 traceparent 헤더를 붙인다.
    쿼리 본문은 span에 담기지 않는다. IAM 인증(SigV4)과 함께 동작한다: 서명은 build_request로 만든 요청에 send 전에 하고
    (_sign), 계측은 그 뒤 transport(HTTPTransport.handle_request)에서 traceparent를 더한다. 그래서 traceparent는 서명한 헤더
    목록(SignedHeaders)에 없고, Neptune은 SignedHeaders에 든 헤더만으로 서명을 다시 계산하므로 더한 헤더가 서명을 깨지 않는다.
    """

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
    @skip_entry_log("reads configuration at startup")
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

    @skip_entry_log("gremlin calls are covered by repository entry lines")
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
            _failed(type(exc).__name__, exc)
            raise NeptuneUnavailable("Neptune response unavailable") from exc
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            _failed("NEPTUNE_RESPONSE_INVALID", None, response.status_code)
            raise NeptuneUnavailable("Neptune returned invalid JSON") from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            _failed("NEPTUNE_RESPONSE_INVALID", None, response.status_code)
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
                    # 호출 측이 선언한 계약 검증 실패(충돌 등). 코드는 우리 쿼리가 정한 고정 낱말이다
                    _failed(
                        "NEPTUNE_QUERY_REJECTED", None, response.status_code, rejection
                    )
                    raise NeptuneQueryRejected(rejection)
            _failed("NEPTUNE_QUERY_FAILED", None, response.status_code)
            raise NeptuneUnavailable(
                f"Neptune query failed (HTTP {response.status_code})"
            )
        if not isinstance(status, dict) or type(status.get("code")) is not int:
            _failed("NEPTUNE_RESPONSE_INVALID", None, response.status_code)
            raise NeptuneUnavailable("Neptune success status is missing")
        result = payload.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            _failed("NEPTUNE_RESPONSE_INVALID", None, response.status_code)
            raise NeptuneUnavailable("Neptune result data must be an array")
        return data

    def _sign(self, request: httpx.Request) -> None:
        try:
            credentials = self._session.get_credentials() if self._session else None
            if credentials is None:
                _failed("AWS_CREDENTIALS_UNAVAILABLE", None)
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
            _failed(type(exc).__name__, exc)
            raise NeptuneUnavailable("AWS request signing failed") from exc

    @skip_entry_log("closes the owned http client")
    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _failed(
    error_type: str,
    failure: BaseException | None,
    status: int | None = None,
    code: str | None = None,
) -> None:
    """Neptune 쿼리 하나의 실패 줄. 예외면 메시지 없는 스택을 붙인다."""
    AppLogger.current().warn(
        "neptune query failed",
        LogField.error_type(error_type),
        LogField.error_code(code),
        LogField.status_code(status),
        LogField.safe_stack_trace(failure),
    )
