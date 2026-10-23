"""업무별 Kafka consumer·publisher와 실제 Neptune/S3/Content/LLM 어댑터의 수명주기."""

import logging
import math
import os
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic

from confluent_kafka import Consumer, Producer
from openai import OpenAI

from app.adapters.llm import ModelLimits, OpenAIProposalModel
from app.adapters.neptune_graph import NeptuneProjectionStore
from app.adapters.neptune_refresh import neptune_refresh_service
from app.adapters.projection_ready import ProjectionReadiness
from app.adapters.refresh_artifacts import S3ArtifactSettings, s3_artifacts
from app.adapters.refresh_inbox import NeptuneRefreshInbox
from app.kafka.consumer import business_worker
from app.kafka.errors import ProcessingStopped
from app.kafka.handlers import ProjectionEventHandler, RefreshEventHandler
from app.kafka.messages import PROJECT_CHANGED, REFRESH_REQUESTED
from app.kafka.polling import consumer_config
from app.kafka.publisher import KafkaPublisher, producer_config
from app.projection.service import GraphProjectionHandler
from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure, ModelSettings
from app.refresh.outbox import RefreshOutboxRelay

logger = logging.getLogger(__name__)

# 작성·보완 지시문이 계획 지시문보다 길어질 수 있는 몫(토큰). 지시문은 수천 자 수준이다.
PLAN_RESERVE = 8192


def enabled(env, key):
    value = env.get(key, "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{key} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class BusinessSettings:
    bootstrap_servers: str
    topics: tuple[str, ...]
    shutdown_seconds: float = 360

    def __post_init__(self):
        consumer_config(self.bootstrap_servers, "graph-rag")
        if (
            not self.topics
            or len(set(self.topics)) != len(self.topics)
            or not set(self.topics) <= {PROJECT_CHANGED, REFRESH_REQUESTED}
        ):
            raise ValueError("Invalid business subscriptions")
        if not (math.isfinite(self.shutdown_seconds) and self.shutdown_seconds > 0):
            raise ValueError("Invalid worker time budgets")

    @classmethod
    def from_env(cls, env: Mapping[str, str]):
        topics = tuple(
            topic
            for topic, key in (
                (PROJECT_CHANGED, "KAFKA_PROJECT_CHANGED_ENABLED"),
                (REFRESH_REQUESTED, "KAFKA_REFRESH_REQUESTED_ENABLED"),
            )
            if enabled(env, key)
        )
        if not topics:
            return None
        if enabled(env, "KAFKA_POLLING_ENABLED"):
            raise ValueError(
                "Business processing and fake polling cannot be enabled together"
            )
        return cls(
            env.get("KAFKA_BOOTSTRAP_SERVERS", ""),
            topics,
            float(env.get("KAFKA_SHUTDOWN_SECONDS", "360")),
        )


class BudgetedModel:
    """종료 신호가 오면 다음 호출을 시작하지 않는 모델·토큰 계수 위임.

    예산을 넘는 요청은 묶음 수만큼 호출이 늘어나므로 요청 전체 기한을 두지 않는다. 호출마다
    OPENAI_TIMEOUT_SECONDS를 지키고, 끝난 묶음은 저장되어 재전달 때 이어서 처리한다.
    """

    def __init__(self, client, limits, stop):
        self.client, self.limits, self.stop = client, limits, stop

    def _model(self):
        if self.stop.is_set():
            raise RefreshFailure(
                Failure("REFRESH_INTERRUPTED", "Refresh execution interrupted", True)
            )
        return OpenAIProposalModel(self.client, self.limits)

    def generate(self, model_input):
        return self._model().generate(model_input)

    def count(self, model_input):
        return self._model().count(model_input)


def real_handlers(topic, publisher, stop, stack, settings):
    if topic == PROJECT_CHANGED:
        store = stack.enter_context(NeptuneProjectionStore.from_env())
        handler = ProjectionEventHandler(GraphProjectionHandler(store))
        return {"FileChanged": handler, "ProjectDeleted": handler}
    artifact_settings = S3ArtifactSettings.from_environment(os.environ)
    artifacts = stack.enter_context(
        s3_artifacts(artifact_settings, region_name=os.environ.get("AWS_REGION"))
    )
    ready = stack.enter_context(ProjectionReadiness.from_env())
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for refresh processing")
    client = stack.enter_context(OpenAI(api_key=api_key, max_retries=0))
    if not os.environ.get("CONTENT_BASE_URL", "").strip():
        raise ValueError("CONTENT_BASE_URL is required for refresh processing")
    # Content 이벤트는 모델을 모른다. 모델은 운영 env가, 프롬프트·스키마 버전은
    # ModelSettings 기본값이 정한다. 잘못된 설정은 요청을 받기 전에 기동을 실패시킨다.
    model_name = os.environ.get("OPENAI_MODEL", "").strip()
    if not model_name:
        raise ValueError("OPENAI_MODEL is required for refresh processing")
    model_settings = ModelSettings(model=model_name)
    # 기본값은 gpt-6-luna의 공식 최대 입력·문맥이다. 호출 하나는 최대 입력의 80%까지만 쓴다.
    limits = ModelLimits(
        model=model_settings.model,
        timeout_seconds=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "240")),
        max_input_tokens=int(os.environ.get("OPENAI_MAX_INPUT_TOKENS", "922000")),
        max_output_tokens=int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "32768")),
        context_window=int(os.environ.get("OPENAI_CONTEXT_WINDOW", "1050000")),
    )
    limits.validate()
    # 계획 입력에는 작성 단계가 덧붙일 계획과 보완 때 덧붙일 이전 후보(각각 출력 한도 이하),
    # 지시문 차이를 남겨 둔다. 실제 호출은 어댑터가 호출마다 다시 센다.
    plan_budget = limits.input_budget - 2 * limits.max_output_tokens - PLAN_RESERVE
    model = BudgetedModel(client, limits, stop)

    def service(user_id):
        return neptune_refresh_service(
            artifacts=artifacts,
            model=model,
            user_id=user_id,
            tokens=model,
            plan_budget=plan_budget,
        )

    def run(request, user_id):
        with service(user_id) as refresh:
            refresh.run(request)

    def fail(request, user_id, failure):
        with service(user_id) as refresh:
            refresh.fail(request, failure)

    return {
        "GraphRefreshRequested": RefreshEventHandler(
            run, ready.check, model_settings, fail
        )
    }


class BusinessPollingRuntime:
    def __init__(
        self,
        settings: BusinessSettings,
        *,
        handler_factory=real_handlers,
        consumer_factory=Consumer,
        producer_factory=Producer,
        outbox_factory=NeptuneRefreshInbox.from_env,
    ):
        self.settings = settings
        self._handlers, self._consumer, self._producer = (
            handler_factory,
            consumer_factory,
            producer_factory,
        )
        self._stop = Event()
        self._outbox_factory = outbox_factory
        self._threads = []
        self._errors = []

    @classmethod
    def from_env(cls, env):
        settings = BusinessSettings.from_env(env)
        return cls(settings) if settings is not None else None

    @property
    def healthy(self):
        return (
            bool(self._threads)
            and not self._errors
            and not self._stop.is_set()
            and all(thread.is_alive() for thread in self._threads)
        )

    def start(self):
        if self._threads:
            raise RuntimeError("Business runtime can only start once")
        try:
            for topic in self.settings.topics:
                ready = Event()
                thread = Thread(
                    target=self._run,
                    args=(topic, ready),
                    name=f"kafka-{topic}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
                if not ready.wait(15) or self._errors:
                    raise RuntimeError("Business Kafka runtime failed to start")
            if REFRESH_REQUESTED in self.settings.topics:
                ready = Event()
                thread = Thread(
                    target=self._relay,
                    args=(ready,),
                    name="refresh-outbox-relay",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
                if not ready.wait(15) or self._errors:
                    raise RuntimeError("Refresh Outbox relay failed to start")
        except BaseException:
            self.close()
            raise

    def _relay(self, ready):
        try:
            with ExitStack() as stack:
                store = stack.enter_context(self._outbox_factory())
                producer = self._producer(
                    producer_config(self.settings.bootstrap_servers)
                )
                stack.callback(producer.flush, 1)
                relay = RefreshOutboxRelay(store, KafkaPublisher(producer))
                ready.set()
                relay.run(self._stop)
                if not self._stop.is_set():
                    raise RuntimeError("Refresh Outbox relay stopped unexpectedly")
        except BaseException as failure:
            self._errors.append(type(failure).__name__)
            logger.error(
                "Refresh Outbox relay failed: reason=%s", type(failure).__name__
            )
        finally:
            ready.set()

    def _run(self, topic, ready):
        try:
            with ExitStack() as stack:
                producer = self._producer(
                    producer_config(self.settings.bootstrap_servers)
                )
                stack.callback(producer.flush, 1)
                publisher = KafkaPublisher(producer)
                handlers = self._handlers(
                    topic, publisher, self._stop, stack, self.settings
                )
                group = (
                    "graph-rag.project-changed"
                    if topic == PROJECT_CHANGED
                    else "graph-rag.graph-refresh-requested"
                )
                config = consumer_config(self.settings.bootstrap_servers, group)
                config["socket.timeout.ms"] = 10000
                consumer = self._consumer(config)
                try:
                    worker = business_worker(
                        consumer, topic, handlers, publisher, self._stop
                    )
                except Exception:
                    consumer.close()
                    raise
                ready.set()
                worker.run(self._stop)
                if not self._stop.is_set():
                    raise RuntimeError("Business worker stopped unexpectedly")
        except ProcessingStopped:
            if not self._stop.is_set():
                self._errors.append("ProcessingStopped")
        except BaseException as failure:
            self._errors.append(type(failure).__name__)
            # Do not log exception messages or chains containing message bodies.
            logger.error(
                "Business Kafka worker failed: topic=%s reason=%s",
                topic,
                type(failure).__name__,
            )
        finally:
            ready.set()

    def close(self):
        self._stop.set()
        deadline = monotonic() + self.settings.shutdown_seconds
        for thread in self._threads:
            thread.join(max(0, deadline - monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("Business Kafka shutdown exceeded budget")
