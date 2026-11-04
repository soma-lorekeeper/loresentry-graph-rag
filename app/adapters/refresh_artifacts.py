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
    BatchObject,
    ContextObject,
    InputObject,
    RequestObject,
    TypedResult,
    context_bytes,
    fingerprint,
    json_bytes,
    omit_default_discovery,
)
from app.logs import AppLogger, LogField, LogFlow, skip_entry_log
from app.manuscript_crypto import EnvelopeError, is_envelope, open_envelope, seal
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import ArtifactRef, Failure, InputSnapshot
from app.refresh.selection import validate_document, validate_input
from app.refresh.serialization import result_from_payload, result_to_payload

# S3에 최신화 입력·실행 스냅샷·결과를 읽고 쓰는 client 레이어다(log.layer)
LOG_LAYER = "client"


@skip_entry_log("raises a safe failure; the caller logs the call result")
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
    max_object_bytes: int = 16 * 1024 * 1024

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
    @skip_entry_log("reads configuration at startup")
    def from_environment(cls, values):
        """명시적으로 전달받은 설정만 사용하며 프로세스 환경을 직접 읽지 않는다."""
        return cls(
            bucket=values.get("GRAPH_REFRESH_COMPLETED_BUCKET", ""),
            input_bucket=values.get("GRAPH_REFRESH_REQUEST_BUCKET", ""),
            prefix=values.get("GRAPH_REFRESH_PREFIX", "refresh"),
            input_prefix=values.get("GRAPH_REFRESH_INPUT_PREFIX", "refresh"),
            max_object_bytes=int(
                values.get("GRAPH_REFRESH_MAX_OBJECT_BYTES", 16 * 1024 * 1024)
            ),
        )


class S3RefreshArtifacts:
    """RefreshArtifactStore 구현. 쓰기는 If-None-Match로 기존 객체를 덮어쓰지 않는다.

    공개 메서드마다 진입 줄(`s3 refresh <대상> get|put call`)을 남기고, 객체 하나를 읽거나 쓸 때마다
    `s3 call completed`(걸린 시간, 응답 상태) 또는 `s3 call failed`(warn, error.type, 상대의 오류 코드) 결과 줄을
    남긴다. 버킷·객체 key(프로젝트·요청 ID가 들어간 경로)와 본문은 남기지 않는다.
    """

    def __init__(self, client, settings: S3ArtifactSettings, key: bytes | None = None):
        self.client, self.settings, self.key = client, settings, key

    @skip_entry_log("returns a copy bound to the manuscript key")
    def with_key(self, key: bytes) -> "S3RefreshArtifacts":
        return type(self)(self.client, self.settings, key)

    @staticmethod
    def _purpose(kind):
        return "refresh_batch" if kind.startswith("batch-") else f"refresh_{kind}"

    def _open(self, request, purpose, body, code="S3_OBJECT_INVALID"):
        if not is_envelope(body):
            return body
        if self.key is None:
            fail(code, "Encrypted S3 object requires a manuscript key")
        try:
            return open_envelope(
                self.key,
                request.job.project_id,
                request.job.request_id,
                purpose,
                body,
            )
        except EnvelopeError:
            fail(code, "Encrypted S3 object cannot be opened")

    def _get(self, request, kind, *, optional=False):
        body = self._read(self._ref(request, kind), optional=optional)
        if body is None:
            return None
        return self._open(request, self._purpose(kind), body)

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
        """전체 객체를 한도 안에서 읽고 본문 스트림은 항상 닫는다.

        객체 하나를 받는 호출(본문까지 다 읽어야 끝난다)이 결과 줄 하나다. 없는 객체(NoSuchKey)는 상대가 답한
        정상 결과라 `s3 call completed`(404)로 남기고, 필요한 객체였을 때만 S3_OBJECT_MISSING으로 실패한다.
        """
        too_large = False
        try:
            with LogFlow.call("s3") as call:
                try:
                    response = self.client.get_object(Bucket=ref.bucket, Key=ref.key)
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "NoSuchKey":
                        raise
                    call.status(404)
                    response = None
                if response is not None:
                    _answered(call, response)
                    stream = response["Body"]
                    try:
                        too_large = (
                            response.get("ContentLength", 0)
                            > self.settings.max_object_bytes
                        )
                        if not too_large:
                            body = stream.read(self.settings.max_object_bytes + 1)
                    finally:
                        stream.close()
        except (ClientError, BotoCoreError) as error:
            self._error(error)
        if response is None:
            if optional:
                return None
            fail("S3_OBJECT_MISSING", "Required S3 object is missing")
        if too_large or len(body) > self.settings.max_object_bytes:
            fail("S3_OBJECT_TOO_LARGE", "S3 object exceeds byte budget")
        if protected and response.get("Metadata", {}).get("sha256") != fingerprint(
            body
        ):
            fail("S3_OBJECT_INVALID", "Stored S3 object checksum differs")
        return body

    def _put(self, request, kind, body):
        ref = self._ref(request, kind)
        if len(body) > self.settings.max_object_bytes:
            fail("S3_OBJECT_TOO_LARGE", "S3 object exceeds byte budget")
        stored = body
        if self.key is not None and kind != "request":
            stored = seal(
                self.key,
                request.job.project_id,
                request.job.request_id,
                self._purpose(kind),
                body,
            )
            if len(stored) > self.settings.max_object_bytes:
                fail("S3_OBJECT_TOO_LARGE", "S3 object exceeds byte budget")
        exists = False
        try:
            # 객체 하나를 쓰는 호출. 이미 있는 객체(PreconditionFailed)는 상대가 답한 정상 결과라
            # s3 call completed(412)로 남기고 아래에서 내용을 비교한다
            with LogFlow.call("s3") as call:
                try:
                    response = self.client.put_object(
                        Bucket=ref.bucket,
                        Key=ref.key,
                        Body=stored,
                        ContentType="application/json",
                        IfNoneMatch="*",
                        Metadata={"sha256": fingerprint(stored)},
                        ChecksumSHA256=base64.b64encode(sha256(stored).digest()).decode(
                            "ascii"
                        ),
                    )
                    _answered(call, response)
                except ClientError as error:
                    if (
                        error.response.get("Error", {}).get("Code")
                        != "PreconditionFailed"
                    ):
                        raise
                    call.status(412)
                    exists = True
        except (ClientError, BotoCoreError) as error:
            self._error(error)
        if exists and self._get(request, kind) != body:
            raise RequestConflict(
                "Stored S3 object differs from proposed content"
            ) from None
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
        if create:
            value = expected.model_dump(mode="json")
            omit_default_discovery(value["request"])
            self._put(request, "request", json_bytes(value))
            return True
        body = self._get(request, "request", optional=True)
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
        AppLogger.current().info("s3 refresh input get call")
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
        body = self._open(request, "refresh_input", body, "S3_INPUT_INVALID")
        try:
            value = InputObject.model_validate_json(body)
        except ValidationError:
            fail("S3_INPUT_INVALID", "S3 input JSON is invalid")
        # v2 문서에는 프로젝트가 없다. fingerprint가 맞았으니 이 요청의 프로젝트 문서다.
        source = InputSnapshot(
            request.job,
            tuple(
                document.snapshot(request.job.project_id)
                for document in value.documents
            ),
        )
        validate_input(request, source)
        return source

    def read_context(self, request):
        AppLogger.current().info("s3 refresh context get call")
        manifest = self._check_request(request)
        body = self._get(request, "context", optional=True)
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
        AppLogger.current().info("s3 refresh context put call")
        request = snapshot.request
        self._validate_context(request, snapshot)
        body = context_bytes(snapshot)
        saved = self._get(request, "context", optional=True)
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
        return self._put(request, "context", body)

    def read_result(self, request):
        AppLogger.current().info("s3 refresh result get call")
        manifest = self._check_request(request)
        body = self._get(request, "result", optional=True)
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
        AppLogger.current().info(
            "s3 refresh result put call",
            LogField.param("refresh_outcome", result.outcome),
        )
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
        return self._put(request, "result", body)

    def read_batch(self, request, index):
        AppLogger.current().info(
            "s3 refresh batch get call", LogField.param("index", index)
        )
        manifest = self._check_request(request)
        body = self._get(request, f"batch-{index:04d}", optional=True)
        if body is None:
            return None
        if not manifest:
            fail("S3_OBJECT_INVALID", "Batch result has no request manifest")
        try:
            value = BatchObject.model_validate_json(body)
        except ValidationError:
            fail("S3_OBJECT_INVALID", "Stored batch JSON is invalid")
        if value.result.job != request.job:
            raise RequestConflict("Stored batch belongs to another request")
        return value.seed_ids, value.result

    def save_batch(self, request, index, seed_ids, result):
        AppLogger.current().info(
            "s3 refresh batch put call",
            LogField.param("index", index),
            LogField.param("seed_ids", seed_ids),
        )
        if result.job != request.job:
            raise RequestConflict("Batch result belongs to another request")
        value = {
            "schema_version": "refresh-batch-v1",
            "seed_ids": list(seed_ids),
            "result": asdict(result),
        }
        try:
            BatchObject.model_validate_json(json_bytes(value))
        except (ValueError, TypeError):
            fail("S3_OBJECT_INVALID", "Batch result is invalid")
        self._check_request(request, create=True)
        return self._put(request, f"batch-{index:04d}", json_bytes(value))


def _answered(call, response) -> None:
    """S3가 답한 응답 상태를 결과 줄에 남긴다. 응답에 상태가 없으면(테스트 대역 등) 남기지 않는다."""
    if not isinstance(response, dict):
        return
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if isinstance(status, int):
        call.status(status)


@contextmanager
@skip_entry_log("builds the s3 client at startup")
def s3_artifacts(settings: S3ArtifactSettings, *, region_name=None, endpoint_url=None):
    """표준 AWS 자격증명 체인으로 클라이언트를 조립하고 항상 종료한다. 자동 재시도는 없다."""
    client = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(
            retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=60
        ),
    )
    try:
        yield S3RefreshArtifacts(client, settings)
    finally:
        client.close()
