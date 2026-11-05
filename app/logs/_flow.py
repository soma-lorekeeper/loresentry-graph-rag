"""흐름 하나의 시작·종료 줄과 span을 여는 로깅 팩토리 LogFlow와, 저장된 trace를 잇는 범위 LogScope(LOG_SCHEMA.md 5절 "만들기",
7절). ai-chat app/logs/_flow.py와 같은 흐름 종류·문구·span이다.

서비스 코드는 흐름의 재료(작업 이름, Kafka 레코드의 위치·헤더, 호출 대상, 발행할 헤더 목록)만 넘기고 `with`로 연다. 로그 객체를
만들고 바인딩하는 일, 걸린 시간을 재는 일, 결과로 레벨을 정하는 일, 시작·종료 줄을 쓰는 일, 우리 span을 열고 지금 문맥으로 두는
일, Kafka 헤더에 traceparent를 넣는 일은 모두 여기서 한다. 할 일이 예외로 끝나도 종료 줄이 남고 span이 닫힌다.

흐름 종류와 여는 span(APM_ARCHITECTURE.md 3절 "우리가 여는 span")
- schedule: 하루 한 번 도는 스케줄 1회. 할 일이 없어도 시작·종료 줄을 남긴다. 예외는 삼키고 실패 줄을 남긴다.
  span `<스케줄 이름>`(INTERNAL, root)
- polling: 짧은 주기 폴링 1회(1초 outbox relay). 회차 span은 회차를 시작할 때 열려 지금 문맥이 된다(회차 안의 Neptune 조회가
  그 자식이다). 그 회차에서 처음 줄이 찍힐 때 시작 줄을 먼저 남기고, 줄이 하나도 없으면 시작·종료 줄을 모두 남기지 않고 회차
  span에 빈 회차 표시를 달아 collector가 그 trace를 버리게 한다. 예외는 삼키고 시작 줄과 실패 줄을 남긴다
- kafka_record: Kafka 레코드 하나. `<event> event received` → `<event> event processed`(loresentry.result.outcome).
  span `<topic> process`(CONSUMER, 레코드 헤더의 traceparent가 부모)
- call: 외부 호출 하나(content, s3, kms, openai, kafka). `<target> call completed` 또는 `<target> call failed`.
  span `<target> call`(INTERNAL, 계측이 없는 대상은 CLIENT). 계측의 CLIENT span(httpx, botocore)이 그 자식이다.
  Kafka 발행(headers를 넘긴 call)은 span `<topic> publish`(PRODUCER)이고, 넘긴 헤더 목록에 그 span의 traceparent를 넣는다
- task: 흐름 안·밖의 끝이 있는 별도 작업. `<name> start` → `<name> completed` 또는 `<name> failed`.
  span `<name>`(INTERNAL, 지금 span의 자식. 흐름 밖에서 시작하면 root)
- worker: 스레드 수명만큼 도는 루프(Kafka polling worker, outbox relay 스레드). task와 같은 줄이지만 span을 열지 않는다.
  루프 안의 레코드·회차가 저마다 span을 연다(LOG_SCHEMA.md 7절 "별도 작업")

흐름이 남기는 지표(app.logs._metrics): kafka_record는 대기 시간(loresentry.kafka.consume.delay)과 처리 시간
(messaging.process.duration), Kafka 발행 call은 발행 시간(messaging.client.operation.duration), 생성형 AI call(model)은 호출 시간
(gen_ai.client.operation.duration)과 사용 토큰(gen_ai.client.token.usage)이다. 실패면 종료 줄과 같은 error.type이 붙는다.
content·S3·KMS call은 지표를 남기지 않는다. content 호출(과 Neptune 쿼리)은 httpx 계측의 http.client.request.duration이 잰다.

span은 시작 줄보다 먼저 열고 걸린 시간을 재기 시작한다. 그래서 span 길이가 종료 줄의 event.duration보다 짧지 않다. 종료 줄은
span을 닫기 전에 그 span 안에서 찍으므로, 종료 줄(호출 결과 줄 포함)의 span.id가 그 span이다. 실패로 끝난 흐름(예외, fail, 상대의
5xx)은 span 상태가 ERROR이고 설명은 그 줄의 error.type이다.

흐름 객체는 진행 중에 알게 된 값을 받는다: add(이후 모든 줄에 붙일 대상 ID), result(종료 줄의 결과 값),
outcome(Kafka 레코드의 결과), status(호출의 응답 상태), fail(예외 없이 끝난 실패의 이유), usage(생성형 AI 호출의 사용 토큰과
응답 모델. 지표에만 쓴다).

시작·종료 줄의 위치(log.logger·log.origin.function·log.layer)는 흐름을 연 서비스 함수다. 실패 줄에는 예외 메시지를
싣지 않는다. 클래스 이름(error.type)과 메시지 없는 스택만 남긴다. 예외가 없는 실패 줄에는 포매터가 호출 위치 스택을 붙인다.
"""

import logging
import re
import time
from collections.abc import Iterable
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from opentelemetry import trace
from opentelemetry.context import Context

from app.logs._fields import LogField, duration
from app.logs._logger import (
    AppLogger,
    Origin,
    bind,
    caller_origin,
    new_logger,
    reset,
)
from app.logs._metrics import (
    genai_called,
    kafka_published,
    kafka_record_finished,
    kafka_record_started,
)
from app.logs._trace import (
    IDLE_POLLING,
    activate,
    current_span,
    detach,
    fill_headers,
    header_parent,
    mark_failed,
    span_ids,
    start_span,
    stored_parent,
    stored_span,
)

# 스케줄·폴링 회차의 시작·종료 줄 문구. 작업은 문구가 아니라 loresentry.schedule.name으로 가린다
SCHEDULE_START = "schedule run start"
SCHEDULE_COMPLETED = "schedule run completed"
SCHEDULE_FAILED = "schedule run failed"
# 상대가 5xx로 답한 호출의 error.type. 응답 상태 자체는 http.response.status_code에 있다
UPSTREAM_SERVER_ERROR = "UPSTREAM_SERVER_ERROR"
# 상대가 준 오류 코드(error.code)로 남겨도 되는 모양. 식별자 모양이 아니면 남기지 않는다
_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


class Outcome(Enum):
    """Kafka 레코드 하나의 처리 결과. 종료 줄의 loresentry.result.outcome이다."""

    # 처리했다. 따로 알리지 않으면 이 값이다
    PROCESSED = "PROCESSED"
    # 이미 처리한 이벤트가 다시 와서 건너뛰었다(inbox 처리 기록)
    DUPLICATE = "DUPLICATE"
    # 이 consumer가 처리하지 않는 종류라 건너뛰었다
    SKIPPED = "SKIPPED"
    # 처리하지 못했다(예외, DLQ)
    FAILED = "FAILED"


class LogFlow:
    """흐름 하나. 아래 클래스 메서드로 만들어 `with`로 연다. 공통으로 흐름 객체의 값 알림 메서드를 가진다."""

    def __init__(self, origin: Origin):
        # 흐름을 연 서비스 함수의 위치. 시작·종료 줄의 위치다
        self._origin = origin
        # 걸린 시간을 재는 시작점(나노초, 흘러간 시간만 재는 시계). span을 연 뒤 __enter__에서 다시 잰다
        self._started = time.perf_counter_ns()
        # 종료 줄에 붙일 결과 값(result)
        self._result: list[LogField] = []
        # add가 연 바인딩. 흐름이 끝날 때 연 반대 순서로 닫는다
        self._bindings: list = []
        # Kafka 레코드의 결과. 알리지 않으면 종료 때 PROCESSED가 된다
        self._outcome: Outcome | None = None
        # 외부 호출의 응답 상태. 모르면 None
        self._status: int | None = None
        # 상대가 준 오류 코드(AWS·OpenAI). 모르면 None
        self._error_code: str | None = None
        # 예외 없이 끝난 실패의 이유(fail). 실패가 아니면 None
        self._failure_type: str | None = None
        # 생성형 AI 호출의 사용 토큰(입력, 출력)과 응답 모델 이름(usage). 지표에만 쓰고, 모르면 None이다
        self._tokens: tuple[int | None, int | None] = (None, None)
        self._response_model: str | None = None
        # 이 흐름이 연 span과, 그 span을 지금 문맥으로 둔 token. 끝날 때 종료 줄을 찍은 뒤 떼고 닫는다. span을 열지 않는
        # 흐름(worker)은 None이다
        self._span: trace.Span | None = None
        self._span_token: object | None = None

    # ------------------------------------------------------------ 흐름 종류

    @classmethod
    def schedule(cls, name: str, *start: LogField | None) -> "LogFlow":
        """하루 한 번 도는 스케줄 작업 1회. 새 trace(회차 span `<name>`)와 작업 이름으로 로그 객체를 만들어 바인딩한다.

        `schedule run start`(시작할 때 아는 값 start)를 남기고, 끝나면 `schedule run completed`(결과·걸린 시간)를
        남긴다. 할 일의 예외(Exception)는 밖으로 내보내지 않고 `schedule run failed`(error, 메시지 없는 스택)로 남긴다.
        """
        return _ScheduleFlow(caller_origin(), name, start)

    @classmethod
    def polling(cls, name: str) -> "LogFlow":
        """짧은 주기 폴링 1회. schedule과 같지만 시작 줄을 그 회차의 첫 줄 직전에 남긴다.

        회차 span은 회차를 시작할 때 열려 지금 문맥이 된다. 그래서 회차 안의 조회(발행 대기 Neptune 쿼리)가 그 자식이 된다.
        줄이 하나도 없는 회차는 시작·종료 줄이 모두 없고, 회차 span에 빈 회차 표시(loresentry.polling.idle)를 달아 collector가
        그 trace(회차 안의 조회 span 포함)를 통째로 버리게 한다. 예외로 끝난 회차는 줄이 없었어도 시작·실패 줄을 남긴다.
        """
        return _PollingFlow(caller_origin(), name)

    @classmethod
    def kafka_record(
        cls,
        topic: str,
        partition: int,
        offset: int,
        headers: Iterable[tuple[str, bytes | str | None]],
        known_types: Iterable[str],
        event: str,
        group: str,
        *,
        timestamp: float | None = None,
    ) -> "LogFlow":
        """Kafka 레코드 하나. 헤더의 traceparent를 이은 consumer span을 열고 그 span으로 로그 객체를 바인딩한다.

        traceparent는 빈 문맥에서 꺼낸다. 지금 문맥(같은 스레드의 다른 흐름)이 부모로 섞이지 않고, 헤더가 없거나 틀리면 새
        trace다. span `<topic> process`(CONSUMER)에는 messaging 속성(system, topic, consumer group, partition, offset,
        작업 종류)을 넣는다. 그 레코드를 처리하는 모든 줄에 topic·partition·offset과 이벤트의 ID·종류·대상(ce_id·ce_type·
        ce_subject)이 붙는다. ce_type은 known_types 안이면 그대로, 밖이면 other이고, ce_id·ce_subject는 표준 표기 UUID일 때만
        남긴다. 시작 줄 `<event> event received`, 종료 줄 `<event> event processed`(결과·걸린 시간)다. 할 일이 예외로 끝나면
        종료 줄을 warn·FAILED·error.type·메시지 없는 스택으로 남기고 span을 ERROR로 표시한 뒤 예외를 그대로 다시 던진다.

        Args:
            headers: 레코드 헤더 (이름, 값) 쌍. 같은 이름이 여럿이면 마지막 값이다.
            known_types: 이 consumer가 처리하는 ce_type.
            event: 문구에 쓰는 이벤트 이름(예: "graph refresh requested").
            group: 레코드를 받은 consumer group. span·지표의 messaging.consumer.group.name이다.
            timestamp: 레코드의 시각(Unix 초, 보통 producer가 만든 시각). 주면 처리를 시작할 때 지금과의 차이를 대기 시간
                지표(loresentry.kafka.consume.delay)로 남긴다. 처리를 마치면 처리 시간(messaging.process.duration)을 남긴다.
                두 지표 모두 topic·partition·consumer group으로 나뉜다.
        """
        return _KafkaRecordFlow(
            caller_origin(),
            topic,
            partition,
            offset,
            headers,
            known_types,
            event,
            group,
            timestamp,
        )

    @classmethod
    def call(
        cls,
        target: str,
        url: str | None = None,
        *,
        server: str | None = None,
        model: str | None = None,
        operation: str | None = None,
        topic: str | None = None,
        headers: list[tuple[str, bytes | None]] | None = None,
    ) -> "LogFlow":
        """외부 호출 하나. 새 로그 객체를 만들지 않고 지금 흐름의 줄로 결과 줄 하나를 남긴다.

        호출을 감싸는 span `<target> call`(INTERNAL)을 열어 지금 문맥으로 둔다. 계측이 여는 CLIENT span(httpx의 content
        호출, botocore의 S3·KMS 호출)이 그 자식이 된다. 계측이 없는 대상(OpenAI Responses API)은 server에 그 호스트를
        넘긴다. 그러면 이 span이 CLIENT가 되고 server.address가 붙어 의존 대상의 span이 빠지지 않는다(LOG_SCHEMA.md 7절
        "외부 호출"). 그 대상이 생성형 AI면 model(설정 값인 모델 이름)도 넘긴다. 계측이 넣었을 gen_ai.provider.name(target)·
        gen_ai.request.model을 이 span에 넣는다. 프롬프트와 모델 응답은 넣지 않는다(LOG_SCHEMA.md 4절). 생성형 AI 호출은 끝날 때
        호출 시간(gen_ai.client.operation.duration)과, usage로 알린 사용 토큰(gen_ai.client.token.usage)을 지표로 남긴다.

        Kafka 발행은 보낼 topic과 헤더 목록(headers)을 넘긴다. span이 `<topic> publish`(PRODUCER, messaging 속성)가 되고,
        그 span을 지금 문맥으로 둔 뒤 헤더 목록에 traceparent를 넣는다(목록에 이미 있던 traceparent는 지운다). 그래서 저장된
        trace의 범위(LogScope) 안에서 보내면 헤더가 그 trace를 잇고, Kafka 레코드 흐름 안에서 보내면(DLQ) 그 레코드의 trace를
        잇는다. 서비스 코드는 traceparent 헤더를 직접 만들지 않는다. 끝나면 발행 시간(messaging.client.operation.duration,
        전달 확인까지)을 지표로 남긴다.

        성공은 `<target> call completed`(걸린 시간, 응답 상태), 상대의 5xx는 같은 문구의 warn과 UPSTREAM_SERVER_ERROR,
        fail이나 예외는 `<target> call failed`(warn, error.type, 예외면 메시지 없는 스택)이고 span이 ERROR다. 예외는 그대로
        다시 던진다. 상대가 답한 실패(botocore ClientError, OpenAI APIStatusError)는 응답 상태와 상대의 오류 코드도 붙인다.
        시작은 이 호출을 부른 client 함수의 진입 줄이다.

        Args:
            target: 호출 대상(content, openai, s3, kms, kafka). 문구와 span 이름에 쓴다.
            url: HTTP 호출의 대상 주소. 주면 url.domain·url.full(쿼리 제외)을 붙인다.
            server: 계측이 없는 대상의 호스트. 주면 span이 CLIENT다.
            model: 계측이 없는 생성형 AI 호출의 모델 이름(설정 값).
            operation: 생성형 AI 호출의 작업 이름(gen_ai.operation.name, 예: chat). model과 함께 넘긴다.
            topic: Kafka 발행의 topic. headers와 함께 넘긴다.
            headers: Kafka 발행의 헤더 목록. 이 함수가 traceparent를 넣는다.
        """
        return _CallFlow(
            caller_origin(), target, url, server, model, operation, topic, headers
        )

    @classmethod
    def task(cls, name: str, *start: LogField | None) -> "LogFlow":
        """흐름 안·밖의 끝이 있는 별도 작업. 새 로그 객체를 만들지 않고 지금 흐름의 줄로 남긴다.

        작업의 span `<name>`(INTERNAL)을 열어 지금 문맥으로 둔다. 지금 span의 자식이고, 흐름 밖에서 시작한 작업은 root다.
        `<name> start`(시작할 때 아는 값)를 남기고 끝나면 `<name> completed`(결과 값·걸린 시간)를 남긴다. fail로 알린
        실패는 `<name> failed`(warn), 예외는 `<name> failed`(error, 메시지 없는 스택)이고 span이 ERROR다. 예외는 다시 던진다.
        """
        return _TaskFlow(caller_origin(), name, start, True)

    @classmethod
    def worker(cls, name: str, *start: LogField | None) -> "LogFlow":
        """스레드 수명만큼 도는 루프 하나(Kafka polling worker, outbox relay 스레드). 줄은 task와 같다.

        span은 열지 않는다. 스레드가 끝날 때까지(파드 종료까지) 닫히지 않는 span은 APM에 그 루프 전체가 transaction 하나로
        남을 뿐이고, 루프 안의 레코드·회차가 저마다 자기 span(consumer span, 회차 span)을 연다(LOG_SCHEMA.md 7절 "별도 작업").
        """
        return _TaskFlow(caller_origin(), name, start, False)

    # ------------------------------------------------------------ 진행 중에 알리는 값

    def add(self, *fields: LogField | None) -> "LogFlow":
        """진행 중에 알게 된 대상 ID(project_id 등)를 이 흐름의 이후 모든 줄과 종료 줄에 붙인다."""
        self._bindings.append(bind(AppLogger.current()._derive(*fields)))
        return self

    def result(self, *fields: LogField | None) -> "LogFlow":
        """종료 줄에 붙일 결과 값(건수 등). 여러 번 부르면 모두 붙는다."""
        self._result.extend(field for field in fields if field is not None)
        return self

    def outcome(self, outcome: Outcome) -> "LogFlow":
        """Kafka 레코드의 결과(건너뜀, 실패 등). 알리지 않으면 PROCESSED다."""
        self._outcome = outcome
        return self

    def status(self, status: int) -> "LogFlow":
        """외부 호출의 응답 상태. 5xx면 결과 줄이 warn이 된다."""
        self._status = status
        return self

    def fail(self, error_type: str) -> "LogFlow":
        """예외 없이 끝난 실패를 알린다. 종료 줄이 warn이 되고 이 값이 error.type이다."""
        self._failure_type = error_type
        return self

    def usage(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        response_model: str | None = None,
    ) -> "LogFlow":
        """생성형 AI 호출의 사용 토큰(공급자가 센 입력·출력)과 응답 모델 이름. 호출이 끝날 때 지표에 쓴다.

        줄에는 붙지 않는다(줄의 토큰 수는 result로 남긴다). 0 이상의 정수가 아닌 토큰 수와 문자열이 아닌 모델 이름은 버린다.
        """
        # 0 이상의 정수(bool 제외)만 토큰 수다
        self._tokens = tuple(
            tokens if type(tokens) is int and tokens >= 0 else None
            for tokens in (input_tokens, output_tokens)
        )
        if isinstance(response_model, str) and response_model:
            self._response_model = response_model
        return self

    # ------------------------------------------------------------ 내부

    def __enter__(self) -> "LogFlow":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def _open_span(
        self,
        name: str,
        kind: trace.SpanKind,
        parent: Context | None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """span을 열어 지금 문맥으로 두고 걸린 시간을 재기 시작한다. parent가 None이면 지금 span의 자식(없으면 root)이다.

        span을 먼저 열고 시계를 잰다. 반대 순서면 span을 여는 시간이 event.duration에만 들어가 span 길이와 어긋난다.
        """
        self._span = start_span(name, kind, parent, attributes)
        self._span_token = activate(self._span)
        self._started = time.perf_counter_ns()

    def _close_span(self) -> None:
        """지금 문맥에서 span을 떼고 닫는다. 종료 줄을 찍은 뒤에 부른다."""
        if self._span_token is not None:
            detach(self._span_token)
        if self._span is not None:
            self._span.end()

    def _elapsed(self) -> float:
        """span을 연 뒤(span이 없으면 흐름을 만든 뒤) 지금까지 걸린 초. 지표의 시간이다."""
        return (time.perf_counter_ns() - self._started) / 1_000_000_000

    def _error_type(self, failure: BaseException | None) -> str | None:
        """지표의 error.type. 종료 줄과 같다: 예외면 그 클래스 이름, 아니면 fail로 알린 이유(상대의 5xx면
        UPSTREAM_SERVER_ERROR). 실패가 아니면 None이다."""
        if failure is not None:
            return type(failure).__name__
        return self._failure_type

    def _failed(self, failure: BaseException | None) -> None:
        """이 흐름의 span을 실패로 표시한다. 설명은 종료 줄의 error.type(예외 클래스 이름, 없으면 fail로 알린 이유)이다."""
        if self._span is not None:
            error_type = type(failure).__name__ if failure is not None else None
            mark_failed(self._span, error_type or self._failure_type, failure)

    def _end(
        self,
        level: int,
        message: str,
        failure: BaseException | None = None,
        stack: bool = False,
    ) -> None:
        """종료 줄 하나. 결과 값, Kafka 결과, 응답 상태, 실패 이유, 걸린 시간을 붙인다.

        지금 바인딩된 객체로 찍으므로 흐름 문맥과 add로 더한 ID가 함께 붙는다. 이 흐름의 span이 아직 지금 문맥이라 줄의
        span.id가 그 span이다.

        Args:
            stack: 예외의 메시지 없는 스택을 붙일지. 예외 없는 실패 줄은 포매터가 호출 위치 스택을 붙인다.
        """
        fields: list[LogField | None] = list(self._result)
        if self._outcome is not None:
            fields.append(LogField.result("outcome", self._outcome))
        if self._status is not None:
            fields.append(LogField.status_code(self._status))
        error_type = type(failure).__name__ if failure is not None else None
        fields.append(LogField.error_type(error_type or self._failure_type))
        fields.append(LogField.error_code(self._error_code))
        if stack:
            fields.append(LogField.safe_stack_trace(failure))
        fields.extend(self._extra_end_fields())
        elapsed = (time.perf_counter_ns() - self._started) / 1_000_000_000
        fields.append(duration(elapsed))
        AppLogger.current()._emit(level, message, fields, self._origin)

    def _extra_end_fields(self) -> list[LogField | None]:
        """흐름 종류가 종료 줄에 더하는 필드(호출의 url.* 등)."""
        return []

    def _close_bindings(self) -> None:
        """add가 연 바인딩을 연 반대 순서로 닫는다."""
        while self._bindings:
            reset(self._bindings.pop())


class _ScheduleFlow(LogFlow):
    """스케줄 작업 1회. 회차 span을 열어 현재 span으로 두고, 그 span으로 로그 객체를 만든다."""

    def __init__(self, origin: Origin, name: str, start: tuple[LogField | None, ...]):
        super().__init__(origin)
        self._name = name
        self._start = start

    def __enter__(self) -> LogFlow:
        # 빈 문맥이 부모라 지금 문맥과 상관없이 새 trace의 root다(LOG_SCHEMA.md 7절 "시작점")
        self._open_span(self._name, trace.SpanKind.INTERNAL, Context())
        logger = new_logger(*span_ids(self._span))._derive(
            LogField.schedule_name(self._name)
        )
        self._token = bind(logger)
        logger._emit(logging.INFO, SCHEDULE_START, self._start, self._origin)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                self._failed(exc)
                self._end(logging.ERROR, SCHEDULE_FAILED, exc, True)
            elif self._failure_type is not None:
                self._failed(None)
                self._end(logging.WARNING, SCHEDULE_FAILED)
            else:
                self._end(logging.INFO, SCHEDULE_COMPLETED)
        finally:
            self._close_bindings()
            reset(self._token)
            self._close_span()
        # Exception은 삼킨다(다음 회차에 다시 한다). 종료 요청 같은 BaseException은 그대로 내보낸다
        return isinstance(exc, Exception)


class _PollingFlow(LogFlow):
    """폴링 1회. 회차 span을 시작할 때 열어 현재 span으로 두고, 시작 줄은 첫 줄 직전에 남긴다.

    줄이 없던 회차는 시작·종료 줄 없이 회차 span에 빈 회차 표시를 단다.
    """

    def __init__(self, origin: Origin, name: str):
        super().__init__(origin)
        self._name = name
        # 이 회차에서 시작 줄을 남겼는가(줄이 하나라도 찍혔는가)
        self._began = False

    def __enter__(self) -> LogFlow:
        # 회차마다 새 trace의 root를 시작할 때 연다. 회차 안의 조회 span이 그 자식이 된다
        self._open_span(self._name, trace.SpanKind.INTERNAL, Context())
        logger = new_logger(*span_ids(self._span))._derive(
            LogField.schedule_name(self._name)
        )

        def begin() -> None:
            # 이 회차의 첫 줄 직전: 시작 줄을 남긴다
            self._began = True
            logger._emit(logging.INFO, SCHEDULE_START, (), self._origin)

        logger._shared_state().before_first_line(begin)
        self._token = bind(logger)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                # 줄이 없던 회차여도 실패 줄이 첫 줄이라 시작 줄이 먼저 남는다
                self._failed(exc)
                self._end(logging.ERROR, SCHEDULE_FAILED, exc, True)
            elif self._failure_type is not None:
                self._failed(None)
                self._end(logging.WARNING, SCHEDULE_FAILED)
            elif self._began:
                self._end(logging.INFO, SCHEDULE_COMPLETED)
            else:
                # 줄이 하나도 없던 빈 회차. 로그처럼 trace도 남지 않도록 회차 span에 표시를 달아, collector가 이
                # trace(회차 안의 발행 대기 조회 span 포함)를 통째로 버리게 한다
                self._span.set_attribute(IDLE_POLLING, True)
        finally:
            self._close_bindings()
            reset(self._token)
            self._close_span()
        return isinstance(exc, Exception)


class _KafkaRecordFlow(LogFlow):
    """Kafka 레코드 하나. 헤더의 trace를 이은 consumer span을 현재 span으로 두고 그 span으로 로그 객체를 만든다."""

    def __init__(
        self,
        origin: Origin,
        topic: str,
        partition: int,
        offset: int,
        headers: Iterable[tuple[str, bytes | str | None]],
        known_types: Iterable[str],
        event: str,
        group: str,
        timestamp: float | None,
    ):
        super().__init__(origin)
        self._topic, self._partition, self._offset = topic, partition, offset
        # 같은 이름의 헤더가 여럿이면 마지막 값이다(Java lastHeader와 같다)
        self._headers = {name: _text(value) for name, value in headers}
        self._known = frozenset(known_types)
        self._event = event
        self._group = group
        # 레코드의 시각(Unix 초). 처리를 시작할 때 대기 시간 지표에 쓴다. 모르면 None
        self._timestamp = timestamp

    def __enter__(self) -> LogFlow:
        self._open_span(
            f"{self._topic} process",
            trace.SpanKind.CONSUMER,
            header_parent(self._headers),
            {
                "messaging.system": "kafka",
                "messaging.destination.name": self._topic,
                "messaging.consumer.group.name": self._group,
                # semantic conventions에서 partition ID는 문자열이다
                "messaging.destination.partition.id": str(self._partition),
                "messaging.kafka.offset": self._offset,
                "messaging.operation.type": "process",
            },
        )
        # 레코드가 만들어진 뒤 처리를 시작하기까지 걸린 시간(loresentry.kafka.consume.delay). 밀림을 보는 지표다
        kafka_record_started(self._topic, self._partition, self._group, self._timestamp)
        logger = new_logger(*span_ids(self._span))._derive(
            LogField.kafka_topic(self._topic),
            LogField.kafka_partition(self._partition),
            LogField.kafka_offset(self._offset),
            LogField.cloudevent_id(self._headers.get("ce_id")),
            LogField.cloudevent_type(self._headers.get("ce_type"), self._known),
            LogField.cloudevent_subject(self._headers.get("ce_subject")),
        )
        self._token = bind(logger)
        logger._emit(logging.INFO, f"{self._event} event received", (), self._origin)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        processed = f"{self._event} event processed"
        try:
            if exc is not None:
                self._outcome = Outcome.FAILED
                self._failed(exc)
                # 원인 줄(받는 쪽이 남긴다)과 겹쳐도 메시지 없는 스택을 붙인다(LOG_SCHEMA.md 5절 "warn·error 스택")
                self._end(logging.WARNING, processed, exc, True)
            else:
                if self._outcome is None:
                    self._outcome = Outcome.PROCESSED
                if self._failure_type is not None or self._outcome is Outcome.FAILED:
                    self._failed(None)
                level = logging.INFO if self._failure_type is None else logging.WARNING
                self._end(level, processed)
        finally:
            self._close_bindings()
            reset(self._token)
            # 처리 시간(messaging.process.duration). 실패면 종료 줄과 같은 error.type이고, 이유 없이 FAILED로 알린 레코드는
            # semantic conventions의 대체 값 _OTHER다
            error_type = self._error_type(exc)
            if error_type is None and self._outcome is Outcome.FAILED:
                error_type = "_OTHER"
            kafka_record_finished(
                self._topic, self._partition, self._group, self._elapsed(), error_type
            )
            self._close_span()
        return False


class _CallFlow(LogFlow):
    """외부 호출 하나. 호출을 감싸는 span을 현재 span으로 두고, 바인딩은 바꾸지 않은 채 결과 줄을 남긴다."""

    def __init__(
        self,
        origin: Origin,
        target: str,
        url: str | None,
        server: str | None,
        model: str | None,
        operation: str | None,
        topic: str | None,
        headers: list[tuple[str, bytes | None]] | None,
    ):
        super().__init__(origin)
        self._target = target
        self._url = url
        # 계측이 없는 대상의 호스트(있으면 CLIENT span)와 생성형 AI 호출의 모델 이름·작업 이름(지표의 gen_ai.operation.name)
        self._server = server
        self._model = model
        self._operation = operation
        # Kafka 발행의 topic과 헤더 목록(있으면 PRODUCER span, 헤더에 traceparent를 넣는다)
        self._topic = topic
        self._headers = headers

    def __enter__(self) -> LogFlow:
        if self._headers is not None:
            # Kafka 발행: confluent-kafka 계측을 쓰지 않으므로 이 span이 발행 span이다. 헤더에는 이 span의 trace를 넣는다
            self._open_span(
                f"{self._topic} publish",
                trace.SpanKind.PRODUCER,
                None,
                {
                    "messaging.system": "kafka",
                    "messaging.destination.name": self._topic,
                    "messaging.operation.type": "send",
                },
            )
            fill_headers(self._headers)
            return self
        # 계측이 없는 대상(server를 넘긴 호출)은 이 span이 그 의존 대상의 CLIENT span이다. 생성형 AI 호출(model)이면
        # 계측이 넣었을 gen_ai 속성(대상 이름이 provider)도 넣는다
        attributes: dict[str, Any] = {}
        if self._server is not None:
            attributes["server.address"] = self._server
        if self._model is not None:
            attributes["gen_ai.provider.name"] = self._target
            attributes["gen_ai.request.model"] = self._model
        kind = trace.SpanKind.INTERNAL
        if self._server is not None:
            kind = trace.SpanKind.CLIENT
        self._open_span(f"{self._target} call", kind, None, attributes or None)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                # 상대가 답한 실패면 응답 상태와 상대의 오류 코드를 남긴다. 연결 실패·시간 초과는 답이 없어 남기지 않는다
                self._status, self._error_code = _answered(exc, self._status)
                self._failed(exc)
                self._end(logging.WARNING, f"{self._target} call failed", exc, True)
            elif self._failure_type is not None:
                self._failed(None)
                self._end(logging.WARNING, f"{self._target} call failed")
            elif self._status is not None and self._status >= 500:
                self._failure_type = UPSTREAM_SERVER_ERROR
                self._failed(None)
                self._end(logging.WARNING, f"{self._target} call completed")
            else:
                self._end(logging.INFO, f"{self._target} call completed")
        finally:
            self._close_bindings()
            self._record(exc)
            self._close_span()
        return False

    def _record(self, failure: BaseException | None) -> None:
        """호출 하나의 지표. Kafka 발행과 생성형 AI 호출만 남긴다. 실패면 종료 줄과 같은 error.type이다.

        content 호출은 httpx 계측의 HTTP 지표(http.client.request.duration)가 이미 재므로 남기지 않는다.
        """
        if self._headers is not None:
            kafka_published(self._topic, self._elapsed(), self._error_type(failure))
        elif self._model is not None:
            genai_called(
                self._target,
                self._operation,
                self._model,
                self._response_model,
                self._elapsed(),
                *self._tokens,
                self._error_type(failure),
            )

    def _extra_end_fields(self) -> list[LogField | None]:
        if self._url is None:
            return []
        try:
            host = urlsplit(self._url).hostname
        except ValueError:
            host = None
        return [LogField.url_domain(host), LogField.url_full(self._url)]


class _TaskFlow(LogFlow):
    """흐름 안·밖의 별도 작업(task)이나 스레드 수명 루프(worker). 바인딩을 바꾸지 않고 지금 흐름의 줄로 시작·종료 줄을 남긴다.

    task는 작업의 span을 열어 현재 span으로 두고, worker는 span을 열지 않는다.
    """

    def __init__(
        self,
        origin: Origin,
        name: str,
        start: tuple[LogField | None, ...],
        span: bool,
    ):
        super().__init__(origin)
        self._name = name
        self._start = start
        # 작업의 span을 여는가(task: 예, worker: 아니오)
        self._opens_span = span

    def __enter__(self) -> LogFlow:
        if self._opens_span:
            self._open_span(self._name, trace.SpanKind.INTERNAL, None)
        AppLogger.current()._emit(
            logging.INFO, f"{self._name} start", self._start, self._origin
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                self._failed(exc)
                self._end(logging.ERROR, f"{self._name} failed", exc, True)
            elif self._failure_type is not None:
                self._failed(None)
                self._end(logging.WARNING, f"{self._name} failed")
            else:
                self._end(logging.INFO, f"{self._name} completed")
        finally:
            self._close_bindings()
            self._close_span()
        return False


class LogScope:
    """시작·종료 줄 없이 저장된 trace를 잇는 범위. `with`로 연다(LOG_SCHEMA.md 7절 "저장했다 나중에 처리").

    범위를 열면 저장된 traceparent를 부모로 한 INTERNAL span을 열어 지금 문맥으로 두고, 그 span으로 로그 객체를 만들어
    바인딩한다. 그래서 범위 안의 모든 줄(부른 repository·publisher 줄 포함)이 저장된 trace와 문맥 필드로 남고, 범위 안에서
    계측이 여는 span(Neptune 쿼리)과 로깅 팩토리의 span(호출, Kafka 발행)이 이 span의 자식이 된다. 발행 헤더도 그 trace를
    잇는다. 닫으면 span을 닫고 이전 바인딩·문맥으로 돌아간다. 예외로 닫히면 span이 ERROR다.

    - stored: 저장했다 나중에 처리하는 일 하나. span `<작업 이름>`
    - outbox_event: outbox 행 하나의 줄과 발행(Neptune InboxEvent의 완료 이벤트 발행, 발행 완료 기록). span
      `outbox <event type>`

    저장된 값이 없거나 형식이 틀리면(추적을 붙이기 전에 쌓인 행) 그 일만 새 trace로 시작한다. 범위를 연 곳(폴링 회차)의
    span과 trace가 다르면 그 span으로 link를 남겨, APM에서 회차와 행 사이를 건너갈 수 있게 한다.
    """

    def __init__(
        self, traceparent: str | None, name: str, context: tuple[LogField | None, ...]
    ):
        # 저장해 둔 traceparent의 span. 형식이 틀리거나 없으면 유효하지 않은 문맥이고, 그때는 새 trace로 시작한다
        self._stored = stored_span(traceparent)
        # 이 범위가 여는 span 이름
        self._name = name
        self._context = context
        self._span: trace.Span | None = None
        self._span_token: object | None = None

    def __enter__(self) -> "LogScope":
        # 범위를 연 곳의 span(폴링 회차). 저장된 trace와 다르면 link로 남긴다
        opener = current_span()
        links: list[trace.Link] = []
        if opener.is_valid and opener.trace_id != self._stored.trace_id:
            links.append(trace.Link(opener))
        # 저장된 span을 부모로 둔 빈 문맥이라 지금 문맥(회차)은 부모로 섞이지 않는다
        self._span = start_span(
            self._name,
            trace.SpanKind.INTERNAL,
            stored_parent(self._stored),
            links=links,
        )
        self._span_token = activate(self._span)
        self._token = bind(new_logger(*span_ids(self._span))._derive(*self._context))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        reset(self._token)
        detach(self._span_token)
        if exc is not None:
            mark_failed(self._span, type(exc).__name__, exc)
        self._span.end()
        return False

    @classmethod
    def stored(
        cls, traceparent: str | None, name: str, *context: LogField | None
    ) -> "LogScope":
        """저장해 둔 traceparent를 부모로 span `<name>`을 열고, 그 span과 문맥 필드로 바인딩한다.

        범위 안에서 만드는 span(쿼리, 호출)이 그 span의 자식이라 저장한 trace에 붙는다. traceparent가 없는 일(추적을 붙이기
        전에 쌓인 일)은 그 일만 새 trace로 남는다.
        """
        return cls(traceparent, name, context)

    @classmethod
    def outbox_event(
        cls,
        traceparent: str | None,
        schedule: str,
        event_id: UUID | str | None,
        event_type: str,
        subject: UUID | str | None,
        *ids: LogField | None,
    ) -> "LogScope":
        """outbox 행 하나의 범위. 행의 traceparent를 부모로 span `outbox <event type>`을 열고, relay 작업 이름, 이벤트의
        ID·종류·대상과 도메인 ID를 붙인다.

        받는 서비스의 Kafka 레코드 줄과 같은 cloudevent 필드라 이벤트 ID 하나로 보낸 줄과 받은 줄을 함께 찾는다. 범위 안의
        발행(LogFlow.call("kafka", ...))이 그 trace를 헤더로 잇는다.

        Args:
            traceparent: 행에 저장한 traceparent. 없으면(추적 전에 쌓인 행) 그 행만 새 trace로 남는다.
            schedule: relay 작업 이름.
            ids: 이벤트가 가리키는 도메인 ID(event key인 project_id 등).
        """
        return cls(
            traceparent,
            f"outbox {event_type}",
            (
                LogField.schedule_name(schedule),
                LogField.cloudevent_id(event_id),
                LogField.cloudevent_type(event_type),
                LogField.cloudevent_subject(subject),
                *ids,
            ),
        )


def _text(value: bytes | str | None) -> str | None:
    """헤더 값을 문자열로. UTF-8이 아니면 대체 문자로 바꾼다(이 값은 UUID·허용 목록 검사 뒤에만 남는다)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return bytes(value).decode("utf-8", errors="replace")


def _answered(
    failure: BaseException, status: int | None
) -> tuple[int | None, str | None]:
    """상대가 답한 실패의 (응답 상태, 상대의 오류 코드).

    - botocore ClientError: response["ResponseMetadata"]["HTTPStatusCode"], response["Error"]["Code"]
    - OpenAI APIStatusError: status_code, code
    - httpx HTTPStatusError: response.status_code
    식별자 모양이 아닌 코드는 남기지 않는다.
    """
    code = None
    response = getattr(failure, "response", None)
    if isinstance(response, dict):
        answered = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(answered, int) and answered > 0:
            status = answered
        code = response.get("Error", {}).get("Code")
    else:
        answered = getattr(failure, "status_code", None)
        if answered is None and response is not None:
            answered = getattr(response, "status_code", None)
        if isinstance(answered, int) and not isinstance(answered, bool):
            status = answered
            code = getattr(failure, "code", None)
    if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
        code = None
    return status, code
