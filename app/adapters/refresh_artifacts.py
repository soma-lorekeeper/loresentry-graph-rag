"""S3 입력 읽기·불변 스냅샷/결과 저장. 클라이언트와 자격증명은 조립 지점이 소유한다."""

import base64
import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from pydantic import ValidationError

from app.adapters.refresh_schema import (
    ContextObject,
    InputObject,
    RequestObject,
    TypedResult,
    fingerprint,
    json_bytes,
)
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import ArtifactRef, Failure, InputSnapshot
from app.refresh.selection import validate_document, validate_input
from app.refresh.serialization import result_from_payload, result_to_payload


def fail(code, message, retryable=False):
    """SDK 응답·자격증명·원고 원문 없이 실패를 전달한다."""
    raise RefreshFailure(Failure(code, message, retryable)) from None


@dataclass(frozen=True)
class S3ArtifactSettings:
    """접근 가능한 버킷·prefix와 객체 바이트 한도. 실제 버킷은 생성하지 않는다."""

    bucket: str
    input_bucket: str
    prefix: str = "refresh"
    input_prefix: str = "refresh"
    max_object_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        for name in (self.bucket, self.input_bucket):
            if not isinstance(name, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name
            ):
                raise ValueError("S3 bucket name is required")
        for prefix in (self.prefix, self.input_prefix):
            if (
                not isinstance(prefix, str)
                or not prefix
                or prefix != prefix.strip("/")
                or any(part in {"", ".", ".."} for part in prefix.split("/"))
            ):
                raise ValueError("A relative nonempty S3 prefix is required")
        if type(self.max_object_bytes) is not int or self.max_object_bytes <= 0:
            raise ValueError("S3 object byte limit must be positive")

    @classmethod
    def from_environment(cls, values):
        """명시적으로 전달받은 설정만 사용하며 프로세스 환경을 직접 읽지 않는다."""
        return cls(
            bucket=values.get("GRAPHRAG_S3_BUCKET", ""),
            input_bucket=values.get("GRAPHRAG_S3_INPUT_BUCKET", ""),
            prefix=values.get("GRAPHRAG_S3_PREFIX", "refresh"),
            input_prefix=values.get("GRAPHRAG_S3_INPUT_PREFIX", "refresh"),
            max_object_bytes=int(
                values.get("GRAPHRAG_S3_MAX_OBJECT_BYTES", 8 * 1024 * 1024)
            ),
        )


class S3RefreshArtifacts:
    """RefreshArtifactStore 구현. 쓰기는 If-None-Match로 기존 객체를 덮어쓰지 않는다."""

    def __init__(self, client, settings: S3ArtifactSettings):
        self.client, self.settings = client, settings

    def _ref(self, request, kind):
        if any(
            not isinstance(value, str) or not value
            for value in (request.job.project_id, request.job.request_id)
        ):
            fail("S3_REQUEST_INVALID", "Nonempty project and request IDs required")
        return ArtifactRef(
            self.settings.bucket,
            "/".join(
                (
                    self.settings.prefix,
                    quote(request.job.project_id, safe=""),
                    quote(request.job.request_id, safe=""),
                    f"{kind}.json",
                )
            ),
        )

    def _read(self, ref, *, optional=False, protected=True):
        """전체 객체를 한도 안에서 읽고 본문 스트림은 항상 닫는다."""
        try:
            response = self.client.get_object(Bucket=ref.bucket, Key=ref.key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                if optional:
                    return None
                fail("S3_OBJECT_MISSING", "Required S3 object is missing")
            self._error(error)
        except BotoCoreError as error:
            self._error(error)
        stream = response["Body"]
        try:
            if response.get("ContentLength", 0) > self.settings.max_object_bytes:
                fail("S3_OBJECT_TOO_LARGE", "S3 object exceeds byte budget")
            body = stream.read(self.settings.max_object_bytes + 1)
            if len(body) > self.settings.max_object_bytes:
                fail("S3_OBJECT_TOO_LARGE", "S3 object exceeds byte budget")
            if protected and response.get("Metadata", {}).get("sha256") != fingerprint(
                body
            ):
                fail("S3_OBJECT_INVALID", "Stored S3 object checksum differs")
            return body
        except BotoCoreError as error:
            self._error(error)
        finally:
            stream.close()

    def _put(self, ref, body):
        if len(body) > self.settings.max_object_bytes:
            fail("S3_OBJECT_TOO_LARGE", "S3 object exceeds byte budget")
        try:
            self.client.put_object(
                Bucket=ref.bucket,
                Key=ref.key,
                Body=body,
                ContentType="application/json",
                IfNoneMatch="*",
                Metadata={"sha256": fingerprint(body)},
                ChecksumSHA256=base64.b64encode(sha256(body).digest()).decode("ascii"),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "PreconditionFailed":
                if self._read(ref) != body:
                    raise RequestConflict(
                        "Stored S3 object differs from proposed content"
                    ) from None
                return ref
            self._error(error)
        except BotoCoreError as error:
            self._error(error)
        return ref

    @staticmethod
    def _error(error):
        if isinstance(error, ClientError):
            code = error.response.get("Error", {}).get("Code")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if code in {
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "ExpiredToken",
            } or status in {401, 403}:
                fail("S3_ACCESS_DENIED", "S3 authentication or permission denied")
            if (
                code
                in {
                    "ConditionalRequestConflict",
                    "SlowDown",
                    "RequestTimeout",
                    "InternalError",
                    "ServiceUnavailable",
                }
                or status >= 500
            ):
                fail("S3_UNAVAILABLE", "S3 operation can be retried", True)
            fail("S3_REQUEST_INVALID", "S3 request or bucket configuration invalid")
        if isinstance(
            error,
            (
                EndpointConnectionError,
                ConnectionClosedError,
                ConnectTimeoutError,
                ReadTimeoutError,
                IncompleteReadError,
                ResponseStreamingError,
            ),
        ):
            fail("S3_UNAVAILABLE", "S3 connection or response interrupted", True)
        fail("S3_CONFIGURATION_INVALID", "S3 client or credentials unavailable")

    def _check_request(self, request, *, create=False):
        expected = RequestObject(schema_version="refresh-request-v1", request=request)
        ref = self._ref(request, "request")
        if create:
            self._put(ref, json_bytes(expected.model_dump(mode="json")))
            return True
        body = self._read(ref, optional=True)
        if body is None:
            return False
        try:
            saved = RequestObject.model_validate_json(body)
        except ValidationError:
            fail("S3_OBJECT_INVALID", "Saved request manifest is invalid")
        if saved.request != request:
            raise RequestConflict(
                "S3 request ID already has different input or settings"
            )
        return True

    def read_input(self, request):
        ref = request.input_ref
        if (
            ref.bucket != self.settings.input_bucket
            or not ref.key.startswith(self.settings.input_prefix + "/")
            or ref
            in tuple(
                self._ref(request, name) for name in ("request", "context", "result")
            )
        ):
            fail(
                "S3_INPUT_LOCATION_INVALID", "Input object is outside configured scope"
            )
        self._check_request(request)
        body = self._read(ref, protected=False)
        if request.job.input_fingerprint != fingerprint(body):
            fail("INPUT_FINGERPRINT_MISMATCH", "S3 input checksum differs from request")
        try:
            value = InputObject.model_validate_json(body)
        except ValidationError:
            fail("S3_INPUT_INVALID", "S3 input JSON is invalid")
        if (value.project_id, value.request_id) != (
            request.job.project_id,
            request.job.request_id,
        ):
            fail("INPUT_MISMATCH", "S3 input belongs to another request")
        source = InputSnapshot(request.job, value.documents)
        validate_input(request, source)
        return source

    def read_context(self, request):
        manifest = self._check_request(request)
        body = self._read(self._ref(request, "context"), optional=True)
        if body is None:
            return None
        if not manifest:
            fail("S3_OBJECT_INVALID", "Execution snapshot has no request manifest")
        try:
            snapshot = ContextObject.model_validate_json(body).snapshot
        except ValidationError:
            fail("S3_OBJECT_INVALID", "Execution snapshot JSON is invalid")
        self._validate_context(request, snapshot)
        return snapshot

    @staticmethod
    def _validate_context(request, snapshot):
        if snapshot.request != request:
            raise RequestConflict("Execution snapshot belongs to another request")
        documents = {d.document_id: d for d in snapshot.documents}
        if (
            len(documents) != len(snapshot.documents)
            or len(set(snapshot.changed_ids)) != len(snapshot.changed_ids)
            or not set(snapshot.changed_ids + snapshot.target_ids) <= documents.keys()
        ):
            fail("S3_OBJECT_INVALID", "Execution snapshot has invalid document IDs")
        validate_input(
            request,
            InputSnapshot(
                request.job, tuple(documents[i] for i in snapshot.changed_ids)
            ),
        )
        for document in snapshot.documents:
            validate_document(request, document)
        if any(
            documents[i].folder_code == "MANUSCRIPT" or documents[i].state != "ACTIVE"
            for i in snapshot.target_ids
        ):
            fail("S3_OBJECT_INVALID", "Snapshot targets must be active settings")

    def save_context(self, snapshot):
        request = snapshot.request
        self._validate_context(request, snapshot)
        value = ContextObject(schema_version="refresh-context-v1", snapshot=snapshot)
        body = json_bytes(value.model_dump(mode="json"))
        saved = self._read(self._ref(request, "context"), optional=True)
        if saved is not None:
            if saved != body:
                raise RequestConflict("Execution snapshot is immutable")
            if not self._check_request(request):
                fail("S3_OBJECT_INVALID", "Execution snapshot has no request manifest")
            return self._ref(request, "context")
        source = self.read_input(request)
        documents = {d.document_id: d for d in snapshot.documents}
        if any(documents.get(d.document_id) != d for d in source.documents):
            fail("S3_OBJECT_INVALID", "Execution snapshot differs from original input")
        self._check_request(request, create=True)
        return self._put(self._ref(request, "context"), body)

    def read_result(self, request):
        manifest = self._check_request(request)
        body = self._read(self._ref(request, "result"), optional=True)
        if body is None:
            return None
        if not manifest:
            fail("S3_OBJECT_INVALID", "Result has no request manifest")
        try:
            result = result_from_payload(json.loads(body))
            TypedResult.model_validate_json(json_bytes({"result": asdict(result)}))
        except (ValueError, TypeError):
            fail("S3_OBJECT_INVALID", "Stored result JSON is invalid")
        if result.job != request.job:
            raise RequestConflict("Stored result belongs to another request")
        return result

    def save_result(self, request, result):
        if result.job != request.job:
            raise RequestConflict("Result belongs to another request")
        saved = self.read_result(request)
        if saved is not None:
            if saved != result:
                raise RequestConflict("Saved result is immutable")
            return self._ref(request, "result")
        snapshot = self.read_context(request)
        try:
            TypedResult.model_validate_json(json_bytes({"result": asdict(result)}))
            body = json_bytes(result_to_payload(request, result, snapshot))
        except (ValueError, TypeError):
            fail("S3_OBJECT_INVALID", "Result or execution snapshot is invalid")
        self._check_request(request, create=True)
        return self._put(self._ref(request, "result"), body)


@contextmanager
def s3_artifacts(settings: S3ArtifactSettings, *, region_name=None, endpoint_url=None):
    """표준 AWS 자격증명 체인으로 클라이언트를 조립하고 항상 종료한다. 자동 재시도는 없다."""
    client = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(
            retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=30
        ),
    )
    try:
        yield S3RefreshArtifacts(client, settings)
    finally:
        client.close()
