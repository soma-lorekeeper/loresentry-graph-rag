"""로깅 팩토리가 기록하는 앱 지표(docs ALERTING.md 7절 "앱 지표 규약"). 계측이 내지 않는 지표만 여기서 만든다.

지표 엔진(SDK MeterProvider, exporter)도 추적처럼 EDOT Python distro가 프로세스 시작 때 설정한다(docker-entrypoint.sh의
opentelemetry-instrument). 그래서 이 모듈은 OpenTelemetry metrics API만 쓴다. 이미지 기본값은 보내지 않음
(OTEL_METRICS_EXPORTER=none, prod 포함)이고, dev overlay가 OTEL_METRICS_EXPORTER=otlp를 주면 span과 같은 collector를 거쳐
Elasticsearch(metrics-*.otel-*)에 쌓인다. distro 없이 띄운 곳(로컬 실행, 평가 CLI)에서는 API가 아무것도 하지 않고, 테스트는
tests/conftest.py가 꽂은 SDK(메모리 reader)를 쓴다.

계측이 이미 내는 지표는 다시 만들지 않는다: 받은 HTTP 요청(FastAPI의 http.server.request.duration·http.server.active_requests),
content 호출·Neptune 쿼리(httpx의 http.client.request.duration. server.address로 나뉜다), 런타임(system_metrics의
process.runtime.cpython.*·cpython.gc.*).

여기서 만드는 지표
- loresentry.event_loop.lag: API 이벤트 루프가 막혀 늦게 깨어난 시간. measure_event_loop가 1초마다 잰다
- loresentry.kafka.handler.queue: Kafka 처리 스레드에 맡긴 레코드 수(state: queued 시작 전, running 처리 중).
  submit_kafka_handler가 센다
- loresentry.kafka.consume.delay: Kafka 레코드가 만들어진 뒤 처리를 시작하기까지 걸린 시간(LogFlow.kafka_record의 시작)
- messaging.process.duration: Kafka 레코드 하나의 처리 시간(LogFlow.kafka_record의 끝)
- messaging.client.operation.duration: Kafka 발행 하나(LogFlow.call("kafka", topic=..., headers=...))
- gen_ai.client.operation.duration, gen_ai.client.token.usage: 생성형 AI 호출 하나와 그 사용 토큰(model을 넘긴 LogFlow.call)

지표는 줄도 span도 만들지 않는다. 속성에는 정해진 값(topic, consumer group, 모델 이름, 예외 클래스 이름)만 넣고 원문 값(ID,
프롬프트, 모델 응답, 메시지 본문)은 넣지 않는다. 값이 None인 속성은 넣지 않는다(SDK가 경고 줄을 남기지 않게).
"""

import asyncio
import contextvars
import time
from collections.abc import AsyncIterator, Callable, Iterable
from concurrent.futures import Executor, Future
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from app.logs._trace import TRACER

# 이 패키지의 지표를 만드는 meter. 계측 이름(scope.name)이 이 패키지가 여는 span과 같은 app.logs다.
# distro가 provider를 먼저 설치하면(이미지) 바로 SDK meter이고, 테스트처럼 import 뒤에 provider를 설치해도 API의 대리
# 객체(proxy)가 그 provider의 지표로 옮겨 간다. 그래서 지표는 모듈을 읽을 때 한 번만 만든다(같은 지표를 다시 만들지 않는다)
_METER = metrics.get_meter(TRACER)

# ------------------------------------------------------------ 버킷(초, 토큰 수)

# 이벤트 루프 지연. 5ms부터 막힘이 길어지는 10초까지
_LAG_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
# messaging semantic conventions가 정한 처리·발행 시간 버킷
_MESSAGING_BUCKETS = (
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
)
# Kafka 대기 시간. 밀림(lag)이 쌓이면 분·시간 단위가 되므로 1시간까지 둔다
_DELAY_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300, 900, 3600)
# GenAI semantic conventions가 정한 토큰 수 버킷(4배씩)
_TOKEN_BUCKETS = (
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
)
# GenAI semantic conventions가 정한 호출 시간 버킷(2배씩, 약 82초까지)
_GENAI_BUCKETS = (
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
)

# ------------------------------------------------------------ 지표

_LOOP_LAG = _METER.create_histogram(
    "loresentry.event_loop.lag",
    unit="s",
    description="How late the API event loop woke up from a timed sleep",
    explicit_bucket_boundaries_advisory=_LAG_BUCKETS,
)
_CONSUME_DELAY = _METER.create_histogram(
    "loresentry.kafka.consume.delay",
    unit="s",
    description="Time from the Kafka record timestamp to the start of its processing",
    explicit_bucket_boundaries_advisory=_DELAY_BUCKETS,
)
_PROCESS_DURATION = _METER.create_histogram(
    "messaging.process.duration",
    unit="s",
    description="Duration of processing operation.",
    explicit_bucket_boundaries_advisory=_MESSAGING_BUCKETS,
)
_PUBLISH_DURATION = _METER.create_histogram(
    "messaging.client.operation.duration",
    unit="s",
    description="Duration of messaging operation initiated by a producer or consumer client.",
    explicit_bucket_boundaries_advisory=_MESSAGING_BUCKETS,
)
_GENAI_DURATION = _METER.create_histogram(
    "gen_ai.client.operation.duration",
    unit="s",
    description="GenAI operation duration.",
    explicit_bucket_boundaries_advisory=_GENAI_BUCKETS,
)
_TOKEN_USAGE = _METER.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Number of input and output tokens used.",
    explicit_bucket_boundaries_advisory=_TOKEN_BUCKETS,
)

# ------------------------------------------------------------ 이벤트 루프 지연


@asynccontextmanager
async def measure_event_loop(interval: float = 1.0) -> AsyncIterator[None]:
    """범위 안에서 이벤트 루프 지연(loresentry.event_loop.lag)을 interval(초)마다 잰다. API 앱의 lifespan이 한 번 연다.

    루프에 task 하나를 띄워 `interval`만큼 잠들고, 예정보다 늦게 깨어난 만큼을 남긴다. 그동안 루프를 막은 코드(async 경로
    안의 동기 IO, 긴 계산)가 있으면 그 시간만큼 늦는다. task는 빈 문맥(contextvars)에서 돌아 지금 span·로그 바인딩이 옮겨
    가지 않고, 줄도 span도 만들지 않는다. 범위를 나가면 task를 취소하고 끝날 때까지 기다린다.
    """
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        _tick(loop, interval), name="event-loop-lag", context=contextvars.Context()
    )
    try:
        yield
    finally:
        task.cancel()
        # 취소가 끝날 때까지 기다린다. asyncio.wait는 그 task의 CancelledError를 다시 던지지 않는다
        # (이 범위를 연 쪽이 취소되는 중이면 그 취소는 그대로 올라간다)
        await asyncio.wait({task})


async def _tick(loop: asyncio.AbstractEventLoop, interval: float) -> None:
    """interval마다 잠들었다 깨어나 늦은 만큼을 남긴다. 취소될 때까지 돈다."""
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        # 잠든 시간보다 더 걸린 만큼이 루프가 막힌 시간이다. 시계 오차로 음수가 되면 0이다
        _LOOP_LAG.record(max(0.0, loop.time() - started - interval))


# ------------------------------------------------------------ Kafka 처리 스레드

# Kafka 처리 스레드(PollingWorker의 ThreadPoolExecutor)에 맡긴 레코드 수. submit_kafka_handler가 고치고 gauge가 읽는다.
# 프로세스의 모든 worker(topic별 consumer 스레드)를 더한 값이다
_handlers = {"queued": 0, "running": 0}
# polling 스레드(맡김), 처리 스레드(시작·끝), 지표 수집 스레드(읽기)가 함께 쓴다
_handlers_lock = Lock()


def _count(queued: int = 0, running: int = 0) -> None:
    """맡긴 레코드 수를 한 번에 고친다. 시작할 때 queued→running을 함께 옮겨 그 사이 읽는 값이 비지 않게 한다."""
    with _handlers_lock:
        _handlers["queued"] += queued
        _handlers["running"] += running


def _observe_handlers(options: CallbackOptions) -> Iterable[Observation]:
    """지표를 모을 때 SDK가 부른다. state별 지금 수다."""
    with _handlers_lock:
        counts = dict(_handlers)
    return [Observation(value, {"state": state}) for state, value in counts.items()]


_METER.create_observable_gauge(
    "loresentry.kafka.handler.queue",
    callbacks=[_observe_handlers],
    unit="{message}",
    description="Kafka records handed to the handler threads, by state",
)


def submit_kafka_handler[Record](
    executor: Executor, handler: Callable[[Record], Any], record: Record
) -> Future:
    """Kafka 레코드 하나를 처리 스레드에 맡긴다(`executor.submit(handler, record)`와 같다). 맡긴 레코드 수를 센다.

    맡긴 때부터 처리 스레드가 시작할 때까지 queued, 시작해서 끝날 때까지(예외로 끝나도) running이다
    (loresentry.kafka.handler.queue). 시작 전에 취소된 일(종료 때 cancel_futures)은 queued에서 뺀다. 처리 스레드가 하나라
    running이 계속 1이면 처리가 밀린다는 뜻이다. 돌려준 Future는 executor.submit의 것이다(결과·예외가 그대로다).
    """

    def run() -> Any:
        _count(queued=-1, running=1)
        try:
            return handler(record)
        finally:
            _count(running=-1)

    def cancelled(future: Future) -> None:
        # 시작 전에 취소된 일은 run이 돌지 않으므로 여기서 queued를 되돌린다
        if future.cancelled():
            _count(queued=-1)

    _count(queued=1)
    try:
        future = executor.submit(run)
    except BaseException:
        # 맡기지 못했다(이미 종료한 executor). 센 것을 되돌리고 그대로 던진다
        _count(queued=-1)
        raise
    future.add_done_callback(cancelled)
    return future


# ------------------------------------------------------------ 흐름이 남기는 지표(_flow가 부른다)


def _kafka(topic: str, group: str | None) -> dict[str, str]:
    """Kafka 소비 지표의 공통 속성. consumer group을 모르면 넣지 않는다."""
    attributes = {"messaging.system": "kafka", "messaging.destination.name": topic}
    if group:
        attributes["messaging.consumer.group.name"] = group
    return attributes


def kafka_record_started(
    topic: str, group: str | None, timestamp: float | None
) -> None:
    """Kafka 레코드 처리를 시작할 때. 레코드의 시각부터 지금까지(loresentry.kafka.consume.delay)를 남긴다.

    timestamp는 레코드의 시각(Unix 초, 보통 producer가 만든 시각)이다. 모르면 남기지 않는다. producer와 이 파드의 시계가
    어긋나 음수가 되면 0이다.
    """
    if timestamp is None:
        return
    _CONSUME_DELAY.record(max(0.0, time.time() - timestamp), _kafka(topic, group))


def kafka_record_finished(
    topic: str, group: str | None, seconds: float, error_type: str | None
) -> None:
    """Kafka 레코드 하나를 다 처리했을 때(messaging.process.duration). 실패면 종료 줄과 같은 error.type을 붙인다."""
    attributes = {**_kafka(topic, group), "messaging.operation.name": "process"}
    if error_type is not None:
        attributes["error.type"] = error_type
    _PROCESS_DURATION.record(seconds, attributes)


def kafka_published(topic: str, seconds: float, error_type: str | None) -> None:
    """Kafka 발행 하나가 끝났을 때(messaging.client.operation.duration). 전달 확인까지 기다린 시간이다."""
    attributes = {
        "messaging.system": "kafka",
        "messaging.operation.name": "send",
        "messaging.operation.type": "send",
        "messaging.destination.name": topic,
    }
    if error_type is not None:
        attributes["error.type"] = error_type
    _PUBLISH_DURATION.record(seconds, attributes)


def genai_called(
    provider: str,
    operation: str | None,
    request_model: str,
    response_model: str | None,
    seconds: float,
    input_tokens: int | None,
    output_tokens: int | None,
    error_type: str | None,
) -> None:
    """생성형 AI 호출 하나가 끝났을 때. 호출 시간(gen_ai.client.operation.duration)과, 알면 사용 토큰
    (gen_ai.client.token.usage, gen_ai.token.type input·output)을 남긴다.

    속성은 공급자, 작업 이름, 요청·응답 모델 이름과 실패의 error.type뿐이다. 프롬프트와 모델 응답은 받지도 않는다.
    """
    attributes = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": request_model,
    }
    if operation is not None:
        attributes["gen_ai.operation.name"] = operation
    if response_model is not None:
        attributes["gen_ai.response.model"] = response_model
    for kind, tokens in (("input", input_tokens), ("output", output_tokens)):
        if tokens is not None:
            _TOKEN_USAGE.record(tokens, {**attributes, "gen_ai.token.type": kind})
    if error_type is not None:
        attributes["error.type"] = error_type
    _GENAI_DURATION.record(seconds, attributes)
