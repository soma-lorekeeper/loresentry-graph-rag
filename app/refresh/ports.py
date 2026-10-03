"""외부 IO를 주입하는 동기식 인터페이스와 어댑터의 구현 계약.

알려진 실패는 RefreshFailure로 전달하고 예상하지 못한 예외는 그대로 전파한다.
요청 식별에는 입력뿐 아니라 선택 설정도 포함한다. 저장은 요청별로 멱등적이어야
하며, 이미 저장한 값과 충돌하는 내용으로 덮어쓰지 않는다. 이 모듈 자체는 IO를 실행하지 않는다.
"""

from typing import Protocol

from app.refresh.models import (
    ArtifactRef,
    Completion,
    DocumentBatch,
    ExecutionSnapshot,
    InputSnapshot,
    JobRecord,
    ModelCandidate,
    ModelInput,
    RefreshRequest,
    RefreshResult,
    RelatedDocuments,
)


class RefreshArtifactStore(Protocol):
    """입력 원문 묶음, 실행 스냅샷, 결과를 읽고 보존하는 저장소 계약."""

    def read_input(self, request: RefreshRequest) -> InputSnapshot:
        """요청의 input_ref가 가리키는 입력 스냅샷을 읽는다.

        Raises:
            RefreshFailure: 입력을 읽거나 해석할 수 없는 경우. 미존재도 실패로 전달한다.
        """
        ...

    def read_context(self, request: RefreshRequest) -> ExecutionSnapshot | None:
        """재실행에 사용할 저장된 실행 스냅샷을 읽는다.

        Returns:
            저장한 스냅샷. 저장한 적이 없을 때만 None을 반환한다.

        Raises:
            RefreshFailure: 저장된 스냅샷의 만료·접근 거부 등으로 읽을 수 없는 경우.
        """
        ...

    def save_context(self, snapshot: ExecutionSnapshot) -> ArtifactRef:
        """실행 스냅샷을 멱등적으로 저장하고 위치를 반환한다.

        같은 요청의 같은 내용은 재사용하며, 다른 내용은 RequestConflict로 거절한다.
        문서 원문과 revision_no를 보존해 재실행에서 조회 시점이 바뀌지 않게 한다.
        """
        ...

    def read_result(self, request: RefreshRequest) -> RefreshResult | None:
        """저장된 결과를 읽고, 저장한 적이 없으면 None을 반환한다.

        저장된 객체를 읽지 못하는 경우는 RefreshFailure로 구분한다.
        """
        ...

    def save_result(
        self, request: RefreshRequest, result: RefreshResult
    ) -> ArtifactRef:
        """결과를 멱등적으로 저장하고 위치를 반환한다.

        요청 식별이나 기존 결과와 내용이 충돌하면 RequestConflict로 거절한다.
        저장 성공 뒤 작업 상태 기록이 실패해도 다음 실행에서 결과를 읽을 수 있어야 한다.
        """
        ...


class RelatedDocumentSource(Protocol):
    """관계 그래프에서 관련 문서와 반영 준비 상태를 조회하는 계약."""

    def find_related(self, request: RefreshRequest) -> RelatedDocuments:
        """요청 범위와 탐색 제한 안에서 명시적 관계를 조회한다.

        Returns:
            관련 문서 ID와 관계, 그래프 반영 준비 상태. 준비되지 않은 상태를
            관련 문서가 없는 READY 결과로 대체하지 않는다.
        """
        ...


class DocumentSource(Protocol):
    """입력 스냅샷에 없는 추가 문서 본문을 조회하는 계약."""

    def fetch_documents(
        self, project_id: str, document_ids: tuple[str, ...]
    ) -> DocumentBatch:
        """접근 범위를 확인하여 문서를 조회하고 누락 ID를 명시한다.

        Args:
            project_id: 어댑터가 신뢰 가능한 인증·인가 정보와 대조할 프로젝트 범위.
            document_ids: 추가로 읽을 문서 식별자.

        Returns:
            조회한 본문·revision_no와 찾지 못한 ID. 누락을 조용히 생략하지 않는다.
        """
        ...


class ProposalModel(Protocol):
    """모델을 호출해 아직 업무 검증을 통과하지 않은 후보를 받는 계약."""

    def generate(self, model_input: ModelInput) -> ModelCandidate:
        """프롬프트와 모델 설정으로 후보 및 사용량을 반환한다.

        호출은 외부 비용을 발생시킬 수 있다. 응답 형식 해석과 업무 유효성 검증은
        구분하며, 후보의 최종 검증은 RefreshRules.validate_candidate가 담당한다.
        """
        ...


class RefreshJobStore(Protocol):
    """요청별 실행 소유권과 복구 지점을 기록하는 저장소 계약."""

    def claim(self, request: RefreshRequest, execution_id: str) -> JobRecord:
        """실행 가능한 작업의 소유권을 원자적으로 획득한다.

        Args:
            request: 동일 요청의 입력과 설정을 확인할 요청 값.
            execution_id: 전달 계층에서 부여한 이번 실행의 식별자.

        Returns:
            획득한 작업 기록. 완료된 작업은 소유권을 새로 얻지 않고 읽을 수 있다.

        Raises:
            JobBusy: 다른 실행이 작업을 소유한 경우.
            RequestConflict: 같은 요청 ID의 입력 또는 설정이 다른 경우.

        실행 임대 만료와 소유권 복구는 어댑터·전달 계층의 책임이다.
        """
        ...

    def load(self, request: RefreshRequest) -> JobRecord | None:
        """현재 작업 기록을 읽고, 기록이 없으면 None을 반환한다."""
        ...

    def checkpoint(self, record: JobRecord) -> None:
        """현재 소유자의 복구 지점과 상태 전이를 기록한다.

        소유권을 확인한 뒤 진행 또는 해제해야 한다. 다른 실행의 변경은
        RequestConflict로 거절한다. 결과 객체 저장과의 단일 트랜잭션은 보장하지 않는다.
        """
        ...


class RefreshCompletionPublisher(Protocol):
    """저장된 결과의 위치와 처리 결과를 완료 메시지로 발행하는 계약."""

    def publish(self, completion: Completion) -> None:
        """완료 메시지를 발행한다.

        수신 측에 전달된 뒤 확인 응답만 유실될 수 있다. 재실행 시 중복 발행이
        가능하므로 수신 측도 요청 식별을 기준으로 중복을 처리해야 한다.
        """
        ...
