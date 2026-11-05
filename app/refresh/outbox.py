"""완료 Inbox와 원자적으로 저장한 발행 대기를 Kafka로 전달한다."""

from threading import Event

from app.kafka.errors import DeliveryError
from app.kafka.messages import completion_event_id
from app.logs import AppLogger, LogField, LogFlow, LogScope, skip_entry_log
from app.refresh.errors import RefreshFailure
from app.refresh.ports import RefreshCompletionPublisher, RefreshOutbox

# relay 작업 이름(loresentry.schedule.name). 회차의 모든 줄과 행마다의 줄에 붙는다
SCHEDULE = "refresh-outbox-relay"
# 완료 이벤트의 ce_type. 받는 서비스(content)의 Kafka 레코드 줄과 같은 값이다
EVENT_TYPE = "GraphRefreshCompleted"


class RefreshOutboxRelay:
    def __init__(self, store: RefreshOutbox, publisher: RefreshCompletionPublisher):
        self.store, self.publisher = store, publisher

    def drain(self, stop: Event) -> int:
        """발행 대기를 오래된 순으로 보낸다. 보낸 행 수를 돌려준다.

        행마다 그 완료를 기록한 요청 처리의 trace(행에 저장한 traceparent)를 부모로 한 범위(LogScope.outbox_event)를 열고
        그 안에서 발행하고 발행 완료를 기록한다. 행마다의 줄, 발행 span, Neptune 쿼리 span이 그 trace로 남고, 발행 헤더도
        그 trace를 이어 받는 서비스가 같은 trace를 잇는다. relay 회차 trace에는 회차 span과 발행 대기 조회만 남는다.
        """
        entries = self.store.pending()
        if entries:
            # 보낼 행이 있는 회차의 첫 줄. 이 줄 직전에 회차의 시작 줄이 남는다
            AppLogger.current().info(
                "refresh outbox batch start", LogField.param("rows", len(entries))
            )
        sent = 0
        for entry in entries:
            if stop.is_set():
                break
            completion = entry.completion
            with LogScope.outbox_event(
                entry.traceparent,
                SCHEDULE,
                completion_event_id(completion),
                EVENT_TYPE,
                completion.job.request_id,
                LogField.project_id(completion.job.project_id),
                LogField.refresh_request_id(completion.job.request_id),
            ):
                self.publisher.publish(completion, occurred_at=entry.created_at)
                # 전달 확인 전에는 SENT로 바꾸지 않는다. ack/DB 응답 유실은 중복 발행 가능.
                self.store.mark_sent(entry)
            sent += 1
        return sent

    @skip_entry_log(
        "polls every second; logs start and end lines only in rounds with rows"
    )
    def run(self, stop: Event) -> None:
        """종료 신호까지 1초 주기로 발행 대기를 보낸다.

        회차 하나가 흐름 하나(LogFlow.polling)다. 보낼 행이 있던 회차와 실패한 회차만 시작·종료 줄
        (보낸 행 수, 걸린 시간)이 남고, 빈 회차는 줄을 남기지 않는다. 회차 span(발행 대기 조회의 부모)은 빈 회차 표시가 붙어
        collector가 버린다.

        전달·일시 저장소 실패는 회차 실패(warn, error.type, 쉬는 시간)로 남기고 쉬었다가 다시 보낸다.
        재시도할 수 없는 실패와 예상하지 못한 예외는 회차 실패 줄을 남긴 뒤 밖으로 던져 relay를 멈춘다
        (relay 스레드가 실패로 끝나 /health가 503이 된다).
        """
        delay = 1
        while not stop.is_set():
            # relay를 멈출 실패. 회차의 실패 줄을 남긴 뒤 회차 밖에서 다시 던진다
            stopping: Exception | None = None
            deferred = False
            sent = 0
            with LogFlow.polling(SCHEDULE) as relay_round:
                try:
                    sent = self.drain(stop)
                    relay_round.result(LogField.result("published", sent))
                except (DeliveryError, RefreshFailure) as failure:
                    relay_round.fail(type(failure).__name__)
                    if (
                        isinstance(failure, RefreshFailure)
                        and not failure.failure.retryable
                    ):
                        stopping = failure
                    else:
                        deferred = True
                        relay_round.result(LogField.result("backoff_seconds", delay))
                except Exception as failure:
                    relay_round.fail(type(failure).__name__)
                    stopping = failure
            if stopping is not None:
                raise stopping
            if deferred:
                stop.wait(delay)
                delay = min(delay * 2, 30)
            else:
                delay = 1
                if not sent:
                    stop.wait(1)
