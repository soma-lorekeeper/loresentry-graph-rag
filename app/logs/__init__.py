"""로그·trace·앱 지표의 공개 API(LOG_SCHEMA.md 5절 "로그 클래스", 7절 "trace", ALERTING.md 7절 "앱 지표 규약"). ai-chat
app/logs와 같은 이름·출력이다.

서비스 코드는 로깅·추적 로직을 모른다. 인자(값, 헤더 목록)만 넘기고, 로그 객체 만들기·바인딩, 걸린 시간 재기, 결과로 레벨
정하기, 시작·종료 줄 쓰기, 우리 span 열기와 지금 문맥 바꾸기, 저장된 trace 잇기, Kafka 헤더에 traceparent 넣기, 앱 지표
기록하기는 모두 이 패키지가 한다. 추적·지표 엔진(SDK, exporter, 라이브러리 계측)은 EDOT Python distro가 프로세스 시작 때
설정한다(docker-entrypoint.sh의 opentelemetry-instrument).

서비스 코드는 이 패키지에서 아래 이름만 import한다. logging 모듈·print·opentelemetry와 밑줄로 시작하는 내부 모듈
(app.logs._*)은 ruff banned-api(TID251)·T20이 막는다.

- AppLogger: `AppLogger.current().info/warn/error(message, *fields)`, identify, add_to_outcome, trace_id
- LogField: 줄에 붙이는 필드의 정적 팩토리
- LogFlow: 시작·종료 줄과 span이 있는 흐름(schedule, polling, kafka_record, call, task)과 span 없는 스레드 수명 루프(worker).
  Kafka 발행은 `LogFlow.call("kafka", topic=..., headers=...)`다
- LogScope: 시작·종료 줄 없이 저장된 trace를 잇는 범위(stored, outbox_event)
- Outcome: Kafka 레코드의 결과(PROCESSED, DUPLICATE, SKIPPED, FAILED)
- skip_entry_log: 진입 줄 규칙의 예외 표시
- configure_logging: 프로세스 시작점이 한 번 부른다. setup_tracing(app): 앱을 다 조립한 뒤 앱 객체만 넘긴다(FastAPI 계측)
- RequestLogMiddleware: 받은 HTTP 요청의 시작점
- traceparent: 저장했다 나중에 처리하는 일에 함께 저장할 지금의 W3C traceparent
- kafka_client_logger: confluent-kafka 설정의 "logger"(librdkafka 로그를 이 형식으로 받는다)
- measure_event_loop: API 앱의 lifespan이 여는 범위. 이벤트 루프 지연 지표(loresentry.event_loop.lag)를 잰다
- submit_kafka_handler: Kafka 레코드를 처리 스레드에 맡긴다(executor.submit 대신). 맡긴 레코드 수 지표
  (loresentry.kafka.handler.queue)를 센다
- Kafka 처리·발행, 생성형 AI 호출의 지표는 LogFlow(kafka_record, call)가 흐름을 닫을 때 남긴다
"""

from collections.abc import Callable
from typing import TypeVar

from app.logs._fields import LogField
from app.logs._flow import LogFlow, LogScope, Outcome
from app.logs._format import configure_logging, kafka_client_logger
from app.logs._logger import AppLogger
from app.logs._metrics import measure_event_loop, submit_kafka_handler
from app.logs._request import RequestLogMiddleware
from app.logs._trace import setup_tracing, traceparent

__all__ = [
    "AppLogger",
    "LogField",
    "LogFlow",
    "LogScope",
    "Outcome",
    "RequestLogMiddleware",
    "configure_logging",
    "kafka_client_logger",
    "measure_event_loop",
    "setup_tracing",
    "skip_entry_log",
    "submit_kafka_handler",
    "traceparent",
]

_Function = TypeVar("_Function", bound=Callable)


def skip_entry_log(reason: str) -> Callable[[_Function], _Function]:
    """레이어 모듈의 공개 함수인데도 진입 줄을 남기지 않는 함수에 붙인다. 실행할 때는 아무 일도 하지 않는다.

    진입 줄 검사 테스트(tests/test_log_entry_lines.py)가 이 표시가 붙은 함수를 건너뛴다. 예외는 다음과 같은 경우다.
    - 값을 계산하거나 변환하기만 하는 함수(메시지 변환, 쿼리 만들기, 입력 검증), 기동 때 한 번 부르는 조립·설정 함수
    - 짧은 주기로 도는 폴링의 실행 함수와 그 회차마다 부르는 조회(처리할 일이 있을 때만 줄을 남긴다)
    - 바로 앞의 다른 진입 줄이 같은 호출을 이미 설명하는 함수(Neptune 쿼리 하나, Kafka 전송 하나)

    Args:
        reason: 줄을 남기지 않는 이유. 로그 문구처럼 영어로 적는다.
    """

    def mark(function: _Function) -> _Function:
        return function

    return mark
