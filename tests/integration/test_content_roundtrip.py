"""실제 Content HTTP→Outbox→Kafka→GraphRAG→S3 결과 왕복. 모델만 대체한다."""

import json
import os
from time import monotonic, sleep
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from app.adapters.neptune_graph import NeptuneProjectionStore
from app.adapters.neptune_refresh import neptune_refresh_service
from app.adapters.projection_ready import ProjectionReadiness
from app.adapters.refresh_artifacts import S3ArtifactSettings, S3RefreshArtifacts
from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.kafka.business_runtime import BusinessPollingRuntime, BusinessSettings
from app.kafka.handlers import ProjectionEventHandler, RefreshEventHandler
from app.kafka.messages import PROJECT_CHANGED, REFRESH_REQUESTED
from app.projection.service import GraphProjectionHandler
from app.refresh.models import ChangePlan, ModelSettings, WrittenCandidate

pytestmark = [pytest.mark.integration, pytest.mark.kafka, pytest.mark.gremlin]


class NoChangeModel:
    def generate(self, model_input):
        if model_input.schema_version == "refresh-plan-v1":
            return ChangePlan(model_input.target_ids, (), (), ())
        return WrittenCandidate(())


def test_real_content_request_and_completion_roundtrip():
    base = os.environ.get("CONTENT_TEST_BASE_URL")
    endpoint = os.environ.get("GREMLIN_TEST_ENDPOINT")
    bootstrap = os.environ.get("KAFKA_TEST_BOOTSTRAP_SERVERS")
    if not all((base, endpoint, bootstrap)):
        pytest.skip("Set CONTENT_TEST_BASE_URL and disposable Kafka/Gremlin endpoints")
    s3_endpoint = os.environ.get("CONTENT_TEST_S3_ENDPOINT", "http://127.0.0.1:18333")
    s3 = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id="local-test-only",
        aws_secret_access_key="local-test-only",
        config=Config(
            s3={"addressing_style": "path"}, retries={"total_max_attempts": 1}
        ),
    )
    bucket = "refresh-fullstack-test"
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as failure:
        if failure.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3.create_bucket(Bucket=bucket)
    artifacts = S3RefreshArtifacts(s3, S3ArtifactSettings(bucket, bucket))

    def handlers(topic, publisher, stop, stack, settings):
        if topic == PROJECT_CHANGED:
            store = stack.enter_context(NeptuneProjectionStore(endpoint))
            handler = ProjectionEventHandler(GraphProjectionHandler(store))
            return {"FileChanged": handler, "ProjectDeleted": handler}
        ready = stack.enter_context(ProjectionReadiness(endpoint))

        def run(request, user_id, wrapped_key):
            with neptune_refresh_service(
                artifacts=artifacts,
                model=NoChangeModel(),
                user_id=user_id,
                content_base_url=base,
                endpoint=endpoint,
            ) as service:
                service.run(request)

        return {
            "GraphRefreshRequested": RefreshEventHandler(
                run, ready.check, ModelSettings(model="fullstack-no-change")
            )
        }

    runtime = BusinessPollingRuntime(
        BusinessSettings(bootstrap, (PROJECT_CHANGED, REFRESH_REQUESTED)),
        handler_factory=handlers,
        outbox_factory=lambda: NeptuneRefreshInbox(endpoint),
    )
    project = None
    with httpx.Client(
        base_url=base, headers={"X-User-Id": str(uuid4())}, timeout=30
    ) as client:
        try:
            runtime.start()
            response = client.post(
                "/projects", json={"name": "Kafka end to end " + uuid4().hex}
            )
            response.raise_for_status()
            project = response.json()["id"]
            response = client.post(
                f"/projects/{project}/files",
                json={"kind": "document", "title": "원고", "folder_code": "MANUSCRIPT"},
            )
            response.raise_for_status()
            file = response.json()["id"]
            # Content는 지난 최신화 이후 바뀐 ACTIVE 문서를 스스로 골라 보낸다.
            response = client.post(f"/projects/{project}/graph-refresh")
            response.raise_for_status()
            assert response.json()["files"] == [{"file_id": file, "revision_no": 0}]
            request = response.json()["request_id"]
            # Content의 완료 처리는 아직 없으므로 GraphRAG가 저장한 결과로 왕복을 확인한다.
            key = f"refresh/{project}/{request}/result.json"
            deadline = monotonic() + 40
            result = None
            while monotonic() < deadline:
                try:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                except ClientError as failure:
                    if failure.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                        raise
                else:
                    result = json.loads(body)
                    break
                assert runtime.healthy
                sleep(0.1)
            assert result is not None
            assert result["schema_version"] == "refresh-result-v3"
            assert result["outcome"] == "NO_CHANGE"
            assert result["request_id"] == request
            # Generation does not change Content originals.
            assert client.get(f"/files/{file}/content").json()["revision_no"] == 0
        finally:
            runtime.close()
            if project:
                client.post(f"/projects/{project}/trash").raise_for_status()
                client.delete(f"/projects/{project}").raise_for_status()
            s3.close()
