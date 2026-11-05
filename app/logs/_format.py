"""모든 로그 줄(우리 AppLogger 줄, uvicorn·OpenTelemetry·librdkafka 같은 프레임워크 줄)을 ECS JSON 한 줄로 만든다.

LOG_SCHEMA.md 5절 "형식"과 content EcsLogFormatter.java와 같은 필드·순서다. 점 이름(log.origin.function)은 중첩
객체({"log":{"origin":{"function":..}}})로 펼치고, 같은 이름은 먼저 들어간 값이 이긴다. 그래서 필수 필드(log.level,
ecs.version 등)는 줄의 필드가 바꾸지 못한다.

필드
- 항상: @timestamp, log.level, log.logger, message, service.name, service.version, process.thread.name, ecs.version
- trace·user·문맥: 우리 줄은 그 줄을 찍은 객체의 값. 프레임워크 줄은 같은 문맥에 바인딩된 객체의 trace·user와
  정해 둔 문맥 필드(MDC_CONTEXT_KEYS). trace.id·span.id는 줄을 쓰는 순간의 지금 span이 먼저이고, 바인딩된 흐름과 다른
  trace이거나 span이 없으면 흐름의 값이다(AppLogger._effective_trace). 바인딩이 없으면 지금 span의 값이다. logging은 줄을
  찍은 스레드에서 이 포매터를 바로 부르므로 그 순간의 지금 span이 그 줄의 span이다
- 우리 줄: log.origin.function, log.layer, log.package, 줄의 필드, 객체의 문맥 필드(줄의 값이 이긴다), tags
- 프레임워크 줄의 예외(exc_info): error.type(클래스)과 메시지 없는 error.stack_trace. 예외 메시지는 남기지 않는다
- warn·error 줄에는 언제나 error.stack_trace가 있다. 예외 스택이 없으면(우리 줄의 업무 거절·상대 5xx·흐름 fail, 프레임워크의
  예외 없는 경고) 줄을 쓰는 순간의 호출 위치 스택을 남긴다(LOG_SCHEMA.md 5절 "warn·error 스택")

JSON을 만들다 실패해도 줄을 버리지 않고 최소 필드만 담은 줄을 쓴다.
"""

import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime

from app.logs._fields import LogField, log_site_stack
from app.logs._logger import LINE_ATTRIBUTE, Line, bound
from app.logs._trace import current_ids

# Logstash는 "ecs":{...}가 든 줄만 필드로 펼친다(gitops logging/base/logstash/pipelines/app-filter.conf)
ECS_VERSION = "8.11"
# 프레임워크 줄에도 붙이는 문맥 필드(LOG_SCHEMA.md 5절 "문맥 필드와 프레임워크 줄"). 그 밖의 문맥은 우리 줄에만 붙는다
MDC_CONTEXT_KEYS = (
    "http.request.method",
    "url.path",
    "http.route",
    "loresentry.schedule.name",
    "loresentry.kafka.topic",
    "loresentry.kafka.partition",
    "loresentry.kafka.offset",
    "loresentry.cloudevent.id",
    "loresentry.cloudevent.type",
    "loresentry.cloudevent.subject",
)
# 표준 logging 레벨 → log.level. 우리 줄은 INFO·WARN·ERROR만 쓴다
_LEVELS = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}
# INFO 줄에 전체 URL(쿼리 포함)을 남기거나 너무 잦은 라이브러리. WARNING부터만 남긴다.
# openai SDK 3.x는 httpx 대신 httpx2·httpcore2로 보낸다(요청마다 "HTTP Request: POST <URL>" INFO 줄)
_QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "openai",
    "botocore",
    "boto3",
    "urllib3",
)
# librdkafka 로그를 받는 logger 이름. Consumer·Producer 설정의 "logger"로 넘긴다
_KAFKA_LOGGER = "librdkafka"
# EDOT distro가 프로세스 시작 때 자기 평문 stderr handler를 다는 logger(OpenTelemetry SDK·exporter·계측, distro 자신)
_DISTRO_LOGGERS = ("opentelemetry", "elasticotel")

_configured = False


class EcsFormatter(logging.Formatter):
    """LogRecord 하나를 ECS JSON 한 줄로 만드는 포매터. root logger의 handler에 단다."""

    def __init__(self, service_name: str, service_version: str | None):
        super().__init__()
        self._service_name = service_name
        self._service_version = service_version

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(
                self._document(record), ensure_ascii=False, separators=(",", ":")
            )
        except Exception as failure:
            # 줄을 버리지 않는다
            return self._fallback(record, failure)

    def _document(self, record: logging.LogRecord) -> dict:
        root = self._fields(record)
        # warn·error 줄에는 언제나 스택이 있다. 예외 스택(프레임워크 줄의 예외, 우리 줄이 넘긴 메시지 없는 스택)이 없으면 호출
        # 위치 스택이다. 첫 줄은 그 줄의 error.type(없으면 logger 이름)이라 예외 스택과 구별된다
        error = root.get("error")
        error = error if isinstance(error, dict) else {}
        if record.levelno >= logging.WARNING and "stack_trace" not in error:
            label = error.get("type") or record.name
            put(root, "error.stack_trace", log_site_stack(label).entries[0][1])
        return root

    def _fields(self, record: logging.LogRecord) -> dict:
        """호출 위치 스택을 뺀 줄의 필드."""
        root: dict = {}
        put(root, "@timestamp", _timestamp(record.created))
        put(root, "log.level", _LEVELS.get(record.levelno, record.levelname))
        # log.logger: logging logger 이름. 우리 줄은 찍은 모듈이다
        put(root, "log.logger", record.name)
        root["message"] = record.getMessage()
        put(root, "service.name", self._service_name)
        put(root, "service.version", self._service_version)
        put(root, "process.thread.name", record.threadName)
        put(root, "ecs.version", ECS_VERSION)

        line: Line | None = getattr(record, LINE_ATTRIBUTE, None)
        if line is not None:
            # 우리 줄: 찍은 객체의 trace·user, 위치, 줄의 필드, 문맥 필드, tags 순서다
            put(root, "trace.id", line.trace_id)
            put(root, "span.id", line.span_id)
            put(root, "user.id", line.user_id)
            put(root, "log.origin.function", line.origin.function)
            put(root, "log.layer", line.origin.layer)
            put(root, "log.package", line.origin.package)
            for name, value in line.fields:
                put(root, name, value)
            for name, value in line.context:
                put(root, name, value)
            if line.tags:
                put(root, "tags", list(line.tags))
            return root

        # 프레임워크 줄: 같은 문맥에 바인딩된 객체가 있으면 그 trace·user와 정해 둔 문맥 필드를 붙인다
        logger = bound()
        if logger is not None:
            trace_id, span_id = logger._effective_trace()
            put(root, "trace.id", trace_id)
            put(root, "span.id", span_id)
            put(root, "user.id", logger._effective_user())
            for name, value in logger._context_entries():
                if name in MDC_CONTEXT_KEYS:
                    put(root, name, value)
        else:
            trace_id, span_id = current_ids()
            put(root, "trace.id", trace_id)
            put(root, "span.id", span_id)
        if record.exc_info and record.exc_info[1] is not None:
            failure = record.exc_info[1]
            put(root, "error.type", type(failure).__name__)
            stack = LogField.safe_stack_trace(failure)
            if stack is not None:
                put(root, "error.stack_trace", stack.entries[0][1])
        return root

    def _fallback(self, record: logging.LogRecord, failure: Exception) -> str:
        """정상 경로가 실패했을 때의 최소 줄. 값은 문자열 연결이 아니라 json으로 이스케이프한다."""
        root: dict = {}
        try:
            put(root, "@timestamp", _timestamp(record.created))
            put(root, "log.level", _LEVELS.get(record.levelno, record.levelname))
            put(root, "log.logger", record.name)
            put(root, "service.name", self._service_name)
            root["message"] = f"log formatting failed: {type(failure).__name__}"
            put(root, "ecs.version", ECS_VERSION)
            return json.dumps(root, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            # 마지막 방어선. 고정 문자열만 쓴다
            return (
                '{"message":"log formatting failed","ecs":{"version":"'
                + ECS_VERSION
                + '"}}'
            )


def put(root: dict, dotted_name: str, value: object) -> None:
    """점 이름을 중첩 객체로 넣는다. 값이 None이면 넣지 않는다.

    먼저 들어간 값이 이긴다: 같은 이름이 이미 있거나 중간 이름에 객체가 아닌 값이 있으면(이름 충돌) 이 값은 버린다.
    """
    if dotted_name is None or value is None:
        return
    parts = dotted_name.split(".")
    node = root
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            return
        node = child
    node.setdefault(parts[-1], value)


def configure_logging(service_name: str) -> None:
    """프로세스의 로그 출력을 ECS JSON으로 맞춘다. 여러 번 불러도 처음 한 번만 적용한다.

    API 모듈을 import할 때(uvicorn이 "Started server process"를 찍기 전) 부른다.
    1. root logger(INFO)에 stdout JSON handler를 단다.
    2. uvicorn·uvicorn.error는 자기 handler를 버리고 root로 올려 보낸다. uvicorn.access(접근 로그)는 끈다.
       접근 로그는 RequestLogMiddleware의 request received·request completed가 대신한다.
    3. httpx·httpcore·openai·botocore·boto3·urllib3는 WARNING부터만 남긴다(httpx INFO 줄에는 전체 URL이 있다).
    4. 처리하지 못한 예외(sys.excepthook, threading.excepthook)를 평문 traceback 대신 로그 줄로 남긴다.
    5. EDOT distro가 단 평문 handler(opentelemetry·elasticotel logger)를 떼고 root로 보낸다. exporter가 collector에 닿지 못할
       때의 경고 같은 줄도 ECS JSON(호출 위치 스택 포함)으로 나간다. 레벨은 distro가 정한 값(OTEL_LOG_LEVEL, 기본 warn) 그대로다.

    Args:
        service_name: Kubernetes 이름(graph-rag-api). service.name이 된다. 버전은 환경 변수 APP_VERSION(이미지 태그)이다.
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EcsFormatter(service_name, os.environ.get("APP_VERSION")))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        server = logging.getLogger(name)
        server.handlers.clear()
        server.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _DISTRO_LOGGERS:
        distro = logging.getLogger(name)
        distro.handlers.clear()
        distro.propagate = True
    sys.excepthook = _uncaught
    threading.excepthook = _uncaught_in_thread
    _configured = True


def kafka_client_logger() -> logging.Logger:
    """librdkafka 로그를 받는 logger. confluent-kafka Consumer·Producer 설정의 "logger"로 넘긴다.

    넘기지 않으면 librdkafka가 stderr에 평문으로 직접 쓴다. 넘기면 poll·flush를 부른 스레드에서 이 logger의
    프레임워크 줄(log.logger: librdkafka)이 된다. Kafka client 내부 일이라 레코드 문맥은 붙지 않는다.
    """
    return logging.getLogger(_KAFKA_LOGGER)


def _uncaught(kind, value, traceback) -> None:
    """프로세스의 처리하지 못한 예외. 종료 요청(KeyboardInterrupt)은 원래 처리기에 맡긴다."""
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, traceback)
        return
    logging.getLogger("app.logs").error(
        "uncaught exception", exc_info=(kind, value, traceback)
    )


def _uncaught_in_thread(args) -> None:
    """스레드의 처리하지 못한 예외. SystemExit는 기본 처리기처럼 조용히 넘긴다."""
    if args.exc_type is SystemExit:
        return
    logging.getLogger("app.logs").error(
        "uncaught exception in thread",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _timestamp(created: float) -> str:
    """UTC ISO-8601, 마이크로초까지, Z로 끝난다."""
    moment = datetime.fromtimestamp(created, tz=UTC)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
