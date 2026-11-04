"""로그·trace의 공개 API(LOG_SCHEMA.md 5절 "로그 클래스", 7절 "trace"). ai-chat app/logs와 같은 이름·출력이다.

서비스 코드는 이 패키지에서 아래 이름만 import한다. logging 모듈·print와 밑줄로 시작하는 내부 모듈
(app.logs._*)은 ruff banned-api(TID251)·T20이 막는다.

- AppLogger: `AppLogger.current().info/warn/error(message, *fields)`, identify, add_to_outcome, trace_id
- LogField: 줄에 붙이는 필드의 정적 팩토리
- LogFlow: 시작·종료 줄이 있는 흐름(schedule, polling, kafka_record, call, task)
- LogScope: 바인딩만 하는 범위(stored, outbox_event)
- Outcome: Kafka 레코드의 결과(PROCESSED, DUPLICATE, SKIPPED, FAILED)
- skip_entry_log: 진입 줄 규칙의 예외 표시
- configure_logging, setup_tracing: 프로세스 시작점이 한 번 부른다
- RequestLogMiddleware: 받은 HTTP 요청의 시작점
- traceparent: 저장했다 나중에 처리하는 일에 함께 저장할 지금의 W3C traceparent
- kafka_client_logger: confluent-kafka 설정의 "logger"(librdkafka 로그를 이 형식으로 받는다)
"""

from collections.abc import Callable
from typing import TypeVar

from app.logs._fields import LogField
from app.logs._flow import LogFlow, LogScope, Outcome
from app.logs._format import configure_logging, kafka_client_logger
from app.logs._logger import AppLogger
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
    "setup_tracing",
    "skip_entry_log",
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
