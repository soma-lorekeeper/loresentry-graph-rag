"""실제 Gremlin에 완료만 저장하고 재시작·발행 실패·응답 유실에서 복구한다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.projection.models import ProjectionChange
from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import ArtifactRef, Completion, Failure, Outcome
from tests.fakes.scenario import scenario
from tests.test_refresh_inbox import request

pytestmark = [pytest.mark.integration, pytest.mark.gremlin]


def test_completion_survives_client_restart_and_rejects_conflicting_request(graph):
    store, query, project = graph
    req = request(project())
    done = Completion(req.job, ArtifactRef("result", "refresh.json"), Outcome.NO_CHANGE)
    with NeptuneRefreshInbox(store.endpoint) as inbox:
        assert inbox.find(req) is None
        inbox.complete(req, done)
        before = query(f"g.V().has('project_id','{req.job.project_id}').valueMap()")
        inbox.complete(req, done)
        assert (
            query(f"g.V().has('project_id','{req.job.project_id}').valueMap()")
            == before
        )
        with pytest.raises(RequestConflict):
            inbox.find(replace(req, discover_related=False))
        with pytest.raises(RequestConflict):
            inbox.complete(req, replace(done, outcome=Outcome.PROPOSED))
    with NeptuneRefreshInbox(store.endpoint) as fresh:
        assert fresh.find(req) == done
        # 같은 request ID라도 다른 프로젝트는 독립적이다.
        assert (
            fresh.find(replace(req, job=replace(req.job, project_id=project()))) is None
        )
    store.apply(
        ProjectionChange(
            event_id=str(uuid4()),
            project_id=req.job.project_id,
            event_type="ProjectDeleted",
        )
    )
    with NeptuneRefreshInbox(store.endpoint) as fresh:
        assert fresh.find(req) == done


def test_refresh_resumes_saved_result_after_publish_failure_without_a_job_store(graph):
    store, _, project = graph
    s = scenario()
    s.request = replace(
        s.request,
        job=replace(s.request.job, project_id=project(), request_id=str(uuid4())),
    )
    s.source = replace(s.source, job=s.request.job)
    s.snapshot = replace(s.snapshot, request=s.request)
    s.result = replace(s.result, job=s.request.job)
    s.artifacts.inputs[s.request.input_ref] = s.source
    s.rules.snapshot, s.rules.result = s.snapshot, s.result
    s.calls.failures["publisher.ack"].append(
        RefreshFailure(Failure("PUBLISH_TIMEOUT", "lost ack", True))
    )
    with NeptuneRefreshInbox(store.endpoint) as inbox:
        s.inbox = inbox
        with pytest.raises(RefreshFailure):
            s.service().run(s.request)
        assert inbox.find(s.request) is None
    s.artifacts.inputs.clear()
    s.documents.documents.clear()
    with NeptuneRefreshInbox(store.endpoint) as fresh:
        s.inbox = fresh
        done = s.service().run(s.request)
        assert fresh.find(s.request) == done
    assert s.calls.names().count("model.generate") == 1
    assert s.publisher.delivered == [done, done]
    with NeptuneRefreshInbox(store.endpoint) as fresh:
        s.inbox = fresh
        assert s.service().run(s.request) == done
    assert s.publisher.delivered == [done, done]


def test_lost_inbox_commit_response_is_recovered_by_lookup(graph):
    store, _, project = graph
    req = request(project())
    done = Completion(req.job, ArtifactRef("result", "refresh.json"), Outcome.NO_CHANGE)
    with httpx.Client(timeout=10) as upstream:

        def lose_ack(request):
            response = upstream.post(
                str(request.url),
                content=request.content,
                headers={"Accept": "application/vnd.gremlin-v3.0+json;types=false"},
            )
            response.raise_for_status()
            raise httpx.ReadTimeout("Committed but lost response", request=request)

        with httpx.Client(transport=httpx.MockTransport(lose_ack)) as client:
            inbox = NeptuneRefreshInbox(store.endpoint, client=client)
            with pytest.raises(RefreshFailure):
                inbox.complete(req, done)
    with NeptuneRefreshInbox(store.endpoint) as fresh:
        assert fresh.find(req) == done


def test_concurrent_completion_keeps_one_immutable_record(graph):
    store, query, project = graph
    req = request(project())
    done = Completion(req.job, ArtifactRef("result", "refresh.json"), Outcome.NO_CHANGE)

    def record(_):
        with NeptuneRefreshInbox(store.endpoint) as inbox:
            for attempt in range(3):
                try:
                    inbox.complete(req, done)
                    return inbox.find(req)
                except RefreshFailure as error:
                    if not error.failure.retryable or attempt == 2:
                        raise

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(record, range(2))) == [done, done]
    assert query(
        f"g.V().hasLabel('InboxEvent').has('project_id','{req.job.project_id}').count()"
    ) == [1]
