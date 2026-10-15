"""예상 가능한 외부 연동·판단 실패와 구현 미완료를 구분하는 예외."""

from app.refresh.models import Failure, Usage, ValidationDiagnostic


class RefreshFailure(Exception):
    """실패 코드와 재시도 가능 여부를 Failure 값으로 전달하는 예상 가능한 실패."""

    def __init__(
        self,
        failure: Failure,
        *,
        diagnostics: tuple[ValidationDiagnostic, ...] = (),
        usage: Usage | None = None,
        candidate_text: str | None = None,
    ):
        self.failure = failure
        self.diagnostics = diagnostics
        self.usage = usage if usage is not None else Usage()
        self.candidate_text = candidate_text
        super().__init__(failure.message)


class RequestConflict(Exception):
    """요청·저장 내용·실행 소유권이 충돌하여 기존 작업을 덮어쓸 수 없는 상태."""


class RulesNotImplemented(NotImplementedError):
    """업무 규칙 미구현을 나타내는 개발 오류. 정상 성공이나 업무 실패 결과가 아니다."""
