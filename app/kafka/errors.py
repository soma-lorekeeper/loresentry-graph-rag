"""메시지 거절·재시도 가능한 업무 실패·전달 불확실성을 구분한다."""


class InvalidEvent(ValueError):
    """받은 값을 포함하지 않는 안전한 계약 검사 코드만 메시지로 사용한다."""


class RetryableProcessingError(RuntimeError):
    """업무 어댑터가 개별 레코드의 일시 장애로 명시적으로 분류한 실패."""


class DeliveryError(RuntimeError):
    """Kafka 발행을 확인하지 못했다. Outbox 대기 또는 DLQ 원본 offset을 유지한다."""


class ProcessingStopped(RuntimeError):
    """종료 요청으로 재시도를 중단했다. 원본 offset을 유지한다."""
