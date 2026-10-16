"""업무별 Kafka consumer·publisher와 실제 Neptune/S3/Content/LLM 어댑터의 수명주기."""

import logging
import math
import os
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
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
from app.refresh.models import Failure
from app.refresh.outbox import RefreshOutboxRelay

logger = logging.getLogger(__name__)


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
    task_seconds: float = 600

    def __post_init__(self):
        consumer_config(self.bootstrap_servers, "graph-rag")
        if (
            not self.topics
            or len(set(self.topics)) != len(self.topics)
            or not set(self.topics) <= {PROJECT_CHANGED, REFRESH_REQUESTED}
        ):
            raise ValueError("Invalid business subscriptions")
        if not all(
            math.isfinite(value) and value > 0
            for value in (self.shutdown_seconds, self.task_seconds)
        ):
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
            float(env.get("KAFKA_REFRESH_TASK_SECONDS", "600")),
        )


class BudgetedModel:
    def __init__(self, client, limits, stop, seconds):
        self.client, self.limits, self.stop = client, limits, stop
        self.deadline = monotonic() + seconds

    def generate(self, model_input):
        remaining = self.deadline - monotonic()
        if self.stop.is_set() or remaining <= 0:
            raise RefreshFailure(
                Failure("REFRESH_INTERRUPTED", "Refresh execution interrupted", True)
            )
        limits = replace(
            self.limits, timeout_seconds=min(self.limits.timeout_seconds, remaining)
        )
        return OpenAIProposalModel(self.client, limits).generate(model_input)


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
    # Profile comes from the immutable Content event, while operator limits remain local.
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for refresh processing")
    client = stack.enter_context(OpenAI(api_key=api_key, max_retries=0))
    if not os.environ.get("CONTENT_BASE_URL", "").strip():
        raise ValueError("CONTENT_BASE_URL is required for refresh processing")

    def run(request, user_id):
        limits = ModelLimits(
            model=request.model_settings.model,
            timeout_seconds=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "240")),
            max_input_tokens=int(os.environ.get("OPENAI_MAX_INPUT_TOKENS", "240000")),
            max_output_tokens=int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "32768")),
            context_window=int(os.environ.get("OPENAI_CONTEXT_WINDOW", "272768")),
        )
        limits.validate()
        model = BudgetedModel(client, limits, stop, settings.task_seconds)
        with neptune_refresh_service(
            artifacts=artifacts, model=model, user_id=user_id
        ) as service:
            service.run(request)

    return {"GraphRefreshRequested": RefreshEventHandler(run, ready.check)}


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
