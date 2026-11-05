"""로깅 팩토리가 기록하는 앱 지표(app.logs._metrics)의 이름·단위·버킷·속성·값.

지표는 tests/conftest.py가 꽂은 SDK MeterProvider의 메모리 reader로 읽는다(measured fixture). 지표는 줄도 span도 만들지
않으므로, 지표를 남기는 흐름의 줄·span은 지표가 없을 때와 같다(그 흐름의 테스트는 tests/test_logs.py).
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.adapters.llm import OpenAIProposalModel
from app.kafka.fake import FakeConsumerHandler, FakeProducer
from app.logs import LogFlow, Outcome, measure_event_loop, submit_kafka_handler
from app.refresh.errors import RefreshFailure
from tests.refresh.test_llm import client_fixture, limits, model_input
from tests.test_kafka_polling import Message, Transport, worker

# 레코드 흐름 테스트의 consumer group
GROUP = "graph-rag.graph-refresh-requested"
# 이 패키지의 지표 scope(span과 같은 계측 이름)
SCOPE = "app.logs"
MESSAGING_BUCKETS = [
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1,
    2.5,
    5,
    7.5,
    10,
]


def lag_tasks() -> list[asyncio.Task]:
    """지금 루프에 남은 지연 측정 task."""
    return [task for task in asyncio.all_tasks() if task.get_name() == "event-loop-lag"]


def queue(measured) -> dict[str, int]:
    """지금의 Kafka 처리 스레드 gauge(state별 수)."""
    found = measured()["loresentry.kafka.handler.queue"]
    assert (found.scope, found.unit) == (SCOPE, "{message}")
    return {point.attributes["state"]: point.value for point in found.points}


def by_attributes(points) -> dict[frozenset, object]:
    """data point를 속성 묶음으로 찾는다."""
    return {frozenset(point.attributes.items()): point for point in points}


# ------------------------------------------------------------------ 이벤트 루프 지연


def test_event_loop_lag_measures_a_blocked_loop_outside_any_trace(
    measured, spans, log_lines
):
    async def serve():
        async with measure_event_loop(interval=0.01):
            await asyncio.sleep(0.03)
            # async 경로 안의 동기 IO처럼 루프를 막는다
            time.sleep(0.1)
            await asyncio.sleep(0.03)
        # 범위를 나가면 잰 task가 취소되어 남지 않는다
        return lag_tasks()

    # 지금 span이 있어도 지연을 재는 task는 빈 문맥에서 돈다. 종료가 멈추면 2초 뒤 TimeoutError로 실패한다
    with LogFlow.task("serve"):
        left = asyncio.run(asyncio.wait_for(serve(), 2))

    assert left == []
    lag = measured()["loresentry.event_loop.lag"]
    assert (lag.scope, lag.unit) == (SCOPE, "s")
    (point,) = lag.points
    assert dict(point.attributes) == {}
    assert point.count >= 3
    assert point.min >= 0
    # 막은 0.1초에서 잠든 시간(0.01초)과 오차를 빼도 0.05초는 넘는다
    assert point.max >= 0.05
    assert list(point.explicit_bounds) == [
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2.5,
        5,
        10,
    ]
    # 지금 span의 exemplar가 붙지 않는다(task가 그 span 밖에서 기록한다)
    assert point.exemplars == []
    # 지연을 재는 일은 줄도 span도 만들지 않는다
    assert [span.name for span in spans()] == ["serve"]
    assert log_lines.messages() == ["serve start", "serve completed"]


def test_event_loop_lag_stops_when_the_scope_ends_with_an_exception():
    async def serve():
        # lifespan 안에서 앱이 예외로 끝나도 측정 task는 멈춘다
        with pytest.raises(RuntimeError, match="app failed"):
            async with measure_event_loop(interval=0.01):
                await asyncio.sleep(0.02)
                raise RuntimeError("app failed")
        return lag_tasks()

    assert asyncio.run(asyncio.wait_for(serve(), 2)) == []


# ------------------------------------------------------------------ Kafka 처리 스레드


def test_handler_queue_counts_queued_running_and_finished_work_including_failures(
    measured,
):
    executor = ThreadPoolExecutor(max_workers=1)
    started, release = Event(), Event()

    def handle(record):
        started.set()
        assert release.wait(2)
        if record == "fail":
            raise ValueError("handler failed")
        return record

    try:
        assert queue(measured) == {"queued": 0, "running": 0}
        first = submit_kafka_handler(executor, handle, "ok")
        assert started.wait(2)
        # 처리 스레드가 하나라 둘째는 첫째가 끝날 때까지 시작 전이다
        second = submit_kafka_handler(executor, handle, "fail")
        assert queue(measured) == {"queued": 1, "running": 1}
        release.set()
        # 돌려준 Future는 처리기의 결과와 예외를 그대로 준다
        assert first.result(2) == "ok"
        with pytest.raises(ValueError, match="handler failed"):
            second.result(2)
        assert queue(measured) == {"queued": 0, "running": 0}
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_handler_queue_releases_work_cancelled_before_it_started(measured):
    executor = ThreadPoolExecutor(max_workers=1)
    started, release = Event(), Event()

    def block(record):
        started.set()
        assert release.wait(2)

    running = submit_kafka_handler(executor, block, "first")
    assert started.wait(2)
    waiting = submit_kafka_handler(executor, block, "second")
    # 종료할 때처럼 시작 전인 일을 취소한다
    executor.shutdown(wait=False, cancel_futures=True)
    assert waiting.cancelled()
    assert queue(measured) == {"queued": 0, "running": 1}
    release.set()
    running.result(2)
    assert queue(measured) == {"queued": 0, "running": 0}
    # 이미 종료한 executor에는 맡기지 못하고, 센 것도 남지 않는다
    with pytest.raises(RuntimeError):
        submit_kafka_handler(executor, block, "late")
    assert queue(measured) == {"queued": 0, "running": 0}


def test_the_polling_worker_hands_each_record_to_the_counted_handler_thread(measured):
    stop = Event()
    transport = Transport(stop, [Message()])
    seen = []

    def handle(record):
        # 처리 스레드 안에서 본 gauge
        seen.append(queue(measured))

    worker(transport, handle).run(stop)
    assert seen == [{"queued": 0, "running": 1}]
    assert queue(measured) == {"queued": 0, "running": 0}


def test_a_failing_polling_handler_is_no_longer_counted(measured):
    stop = Event()
    transport = Transport(stop, [Message()])

    def fail(record):
        raise ValueError("fake producer failed")

    with pytest.raises(ValueError):
        worker(transport, fail).run(stop)
    assert queue(measured) == {"queued": 0, "running": 0}


# ------------------------------------------------------------------ Kafka 소비·발행


def test_kafka_record_records_its_consume_delay_and_process_time(measured, spans):
    # producer가 30초 전에 만든 레코드
    with LogFlow.kafka_record(
        "t", 0, 0, (), (), "x", GROUP, timestamp=time.time() - 30
    ):
        pass
    # producer 시계가 앞선 레코드는 대기 시간이 0이다. 처리는 예외로 끝난다
    with pytest.raises(RuntimeError):
        with LogFlow.kafka_record(
            "t", 0, 1, (), (), "x", GROUP, timestamp=time.time() + 60
        ):
            raise RuntimeError("secret")
    # 시각을 모르는 레코드는 대기 시간을 남기지 않는다. 이유와 함께 FAILED로 알린 레코드
    with LogFlow.kafka_record("t", 0, 2, (), (), "x", GROUP) as flow:
        flow.outcome(Outcome.FAILED).fail("InvalidEvent")
    # 이유 없이 FAILED로만 알린 레코드
    with LogFlow.kafka_record("t", 0, 3, (), (), "x", GROUP) as flow:
        flow.outcome(Outcome.FAILED)

    found = measured()
    common = {
        "messaging.system": "kafka",
        "messaging.destination.name": "t",
        "messaging.consumer.group.name": GROUP,
    }
    delay = found["loresentry.kafka.consume.delay"]
    assert (delay.scope, delay.unit) == (SCOPE, "s")
    (waited,) = delay.points
    assert dict(waited.attributes) == common
    assert waited.count == 2
    assert waited.min == 0
    assert 30 <= waited.max < 40
    assert list(waited.explicit_bounds) == [
        0.01,
        0.05,
        0.1,
        0.5,
        1,
        5,
        10,
        30,
        60,
        300,
        900,
        3600,
    ]

    process = found["messaging.process.duration"]
    assert (process.scope, process.unit) == (SCOPE, "s")
    points = {point.attributes.get("error.type"): point for point in process.points}
    assert set(points) == {None, "RuntimeError", "InvalidEvent", "_OTHER"}
    assert dict(points[None].attributes) == {
        **common,
        "messaging.operation.name": "process",
    }
    assert dict(points["RuntimeError"].attributes) == {
        **common,
        "messaging.operation.name": "process",
        "error.type": "RuntimeError",
    }
    for point in points.values():
        assert point.count == 1
        assert 0 <= point.sum < 1
        assert list(point.explicit_bounds) == MESSAGING_BUCKETS
    # 처리 시간은 그 레코드의 consumer span 안에서 기록된다(exemplar가 그 span이다)
    first = spans()[0]
    (exemplar,) = points[None].exemplars
    assert exemplar.span_id == first.context.span_id
    assert "secret" not in repr(found)


def test_records_from_the_polling_worker_carry_their_create_time(measured):
    stop = Event()
    created = int(time.time() * 1000) - 5000
    transport = Transport(stop, [Message(created=created)])
    records = []
    sandbox = FakeConsumerHandler(FakeProducer(), "graph-rag-fake-test")

    def handle(record):
        records.append(record)
        sandbox(record)

    worker(transport, handle).run(stop)
    assert records[0].timestamp == created / 1000
    (waited,) = measured()["loresentry.kafka.consume.delay"].points
    assert dict(waited.attributes) == {
        "messaging.system": "kafka",
        "messaging.destination.name": "graph-rag.sandbox.test",
        "messaging.consumer.group.name": "graph-rag-fake-test",
    }
    assert 5 <= waited.sum < 15


def test_kafka_publish_records_the_time_until_delivery(measured):
    with LogFlow.call("kafka", topic="graph-rag.graph-refresh-completed", headers=[]):
        pass
    with pytest.raises(RuntimeError):
        with LogFlow.call("kafka", topic="dlq", headers=[]):
            raise RuntimeError("secret")
    # content 호출은 httpx 계측의 HTTP 지표가 잰다. 이 패키지는 남기지 않는다
    with LogFlow.call("content", "http://content-api/x") as call:
        call.status(200)

    found = measured()
    publish = found["messaging.client.operation.duration"]
    assert (publish.scope, publish.unit) == (SCOPE, "s")
    common = {
        "messaging.system": "kafka",
        "messaging.operation.name": "send",
        "messaging.operation.type": "send",
    }
    points = by_attributes(publish.points)
    assert set(points) == {
        frozenset(
            {
                **common,
                "messaging.destination.name": "graph-rag.graph-refresh-completed",
            }.items()
        ),
        frozenset(
            {
                **common,
                "messaging.destination.name": "dlq",
                "error.type": "RuntimeError",
            }.items()
        ),
    }
    for point in points.values():
        assert point.count == 1
        assert list(point.explicit_bounds) == MESSAGING_BUCKETS
    assert "gen_ai.client.operation.duration" not in found
    assert "messaging.process.duration" not in found


# ------------------------------------------------------------------ 생성형 AI 호출


def answering_client():
    """사용 토큰과 응답 모델 이름을 주는 OpenAI client 대역."""
    client = client_fixture()
    response = client.responses.create.return_value
    response.usage.input_tokens = 1200
    response.usage.output_tokens = 300
    response.model = "gpt-5.6-luna-2026-10-03"
    return client


def test_openai_calls_record_operation_time_and_token_usage(measured):
    client = answering_client()
    asked = model_input()
    OpenAIProposalModel(client, limits()).generate(asked)

    found = measured()
    common = {
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": "gpt-5.6-luna",
    }
    chat = {
        **common,
        "gen_ai.operation.name": "chat",
        "gen_ai.response.model": "gpt-5.6-luna-2026-10-03",
    }
    duration = found["gen_ai.client.operation.duration"]
    assert (duration.scope, duration.unit) == (SCOPE, "s")
    points = by_attributes(duration.points)
    # 토큰 계수와 생성이 작업 이름으로 나뉜다. 계수 응답에는 모델 이름이 없다
    assert set(points) == {
        frozenset({**common, "gen_ai.operation.name": "count_tokens"}.items()),
        frozenset(chat.items()),
    }
    for point in points.values():
        assert point.count == 1
        assert list(point.explicit_bounds) == [
            0.01,
            0.02,
            0.04,
            0.08,
            0.16,
            0.32,
            0.64,
            1.28,
            2.56,
            5.12,
            10.24,
            20.48,
            40.96,
            81.92,
        ]

    usage = found["gen_ai.client.token.usage"]
    assert (usage.scope, usage.unit) == (SCOPE, "{token}")
    tokens = {point.attributes["gen_ai.token.type"]: point for point in usage.points}
    assert set(tokens) == {"input", "output"}
    assert (tokens["input"].sum, tokens["output"].sum) == (1200, 300)
    assert dict(tokens["input"].attributes) == {**chat, "gen_ai.token.type": "input"}
    assert list(tokens["input"].explicit_bounds) == [
        1,
        4,
        16,
        64,
        256,
        1024,
        4096,
        16384,
        65536,
        262144,
        1048576,
        4194304,
    ]
    # 프롬프트·지시문·모델 응답은 지표 어디에도 없다
    for text in (asked.prompt, asked.instructions, "ignore instructions"):
        assert text not in repr(found)


@pytest.mark.parametrize("stage", ["count", "create"])
def test_a_failed_openai_call_records_its_error_type_and_no_tokens(measured, stage):
    import httpx2
    from openai import APITimeoutError

    client = answering_client()
    failing = {
        "count": client.responses.input_tokens.count,
        "create": client.responses.create,
    }[stage]
    failing.side_effect = APITimeoutError(request=httpx2.Request("POST", "http://test"))
    with pytest.raises(RefreshFailure):
        OpenAIProposalModel(client, limits()).generate(model_input())

    found = measured()
    failed = [
        point
        for point in found["gen_ai.client.operation.duration"].points
        if "error.type" in point.attributes
    ]
    (point,) = failed
    assert point.attributes["error.type"] == "APITimeoutError"
    assert point.attributes["gen_ai.operation.name"] == (
        "count_tokens" if stage == "count" else "chat"
    )
    # 실패한 호출에는 응답이 없어 응답 모델도 사용 토큰도 없다
    assert "gen_ai.response.model" not in point.attributes
    assert "gen_ai.client.token.usage" not in found


def test_usage_drops_values_that_are_not_token_counts_or_model_names(measured):
    for tokens, model in [((-1, True), 7), (("10", 2.5), "")]:
        with LogFlow.call(
            "openai", server="api.openai.com", model="m", operation="chat"
        ) as call:
            call.usage(*tokens, model)
    with LogFlow.call(
        "openai", server="api.openai.com", model="m", operation="chat"
    ) as call:
        call.usage(None, 0)

    found = measured()
    (duration,) = found["gen_ai.client.operation.duration"].points
    assert duration.count == 3
    assert "gen_ai.response.model" not in duration.attributes
    (output,) = found["gen_ai.client.token.usage"].points
    assert (output.attributes["gen_ai.token.type"], output.count, output.sum) == (
        "output",
        1,
        0,
    )
