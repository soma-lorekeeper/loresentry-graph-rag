"""외부 IO 없이 판단할 업무 규칙의 경계. 현재 모든 기본 메서드는 미구현이다.

테스트는 응답을 지정한 대체 규칙을 명시적으로 주입한다. 운영 기본값이 입력을
조용히 통과시키거나 갱신안을 생성하지 않도록 RulesNotImplemented를 발생시킨다.
"""

from app.refresh.errors import RulesNotImplemented
from app.refresh.models import (
    Chunk,
    DocumentBatch,
    ExecutionSnapshot,
    InputSnapshot,
    ModelCandidate,
    ModelInput,
    RefreshRequest,
    RefreshResult,
    RelatedDocuments,
    Selection,
)


class RefreshRules:
    """입력 검증부터 갱신안 검증까지 맡을 순수 판단 계층의 골격.

    아래 메서드의 반환 설명은 구현할 계약이다. 현재 기본 구현은 모두
    RulesNotImplemented를 발생시키며, DB·네트워크·시간·난수를 직접 조회하지 않는다.
    """

    def validate_input(self, request: RefreshRequest, source: InputSnapshot) -> None:
        """요청과 입력 스냅샷의 사용 조건을 검증할 경계.

        Raises:
            RulesNotImplemented: 입력 검증 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("validate_input")

    def select(
        self, request: RefreshRequest, source: InputSnapshot, related: RelatedDocuments
    ) -> Selection:
        """입력과 관계 조회 결과에서 처리할 문서를 선택할 경계.

        Returns:
            구현 시 처리 대상 ID와 추가 본문 조회가 필요한 ID를 담을 Selection.

        Raises:
            RulesNotImplemented: 문서 선택 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("select")

    def assemble(
        self,
        request: RefreshRequest,
        source: InputSnapshot,
        related: RelatedDocuments,
        selection: Selection,
        fetched: DocumentBatch,
    ) -> ExecutionSnapshot:
        """조회 결과의 범위·상태·누락을 검증하고 실행 입력을 조립할 경계.

        원본의 revision을 보존해야 한다. 추가 조회 자체는 서비스가 IO 포트로 수행한다.

        Returns:
            구현 시 재실행에도 사용할 확정된 ExecutionSnapshot.

        Raises:
            RulesNotImplemented: 조립 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("assemble")

    def chunk(self, snapshot: ExecutionSnapshot) -> tuple[Chunk, ...]:
        """확정된 본문을 출처 정보가 있는 조각으로 나눌 경계.

        Raises:
            RulesNotImplemented: 분할 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("chunk")

    def build_context(
        self, snapshot: ExecutionSnapshot, chunks: tuple[Chunk, ...]
    ) -> str:
        """본문 조각과 출처로 모델에 제공할 문맥 문자열을 구성할 경계.

        Raises:
            RulesNotImplemented: 문맥 구성 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("build_context")

    def build_prompt(self, context: str) -> ModelInput:
        """문맥을 프롬프트 및 버전·모델 설정으로 변환할 경계.

        모델 호출 자체는 ProposalModel의 책임이다.

        Raises:
            RulesNotImplemented: 프롬프트 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("build_prompt")

    def validate_candidate(
        self,
        snapshot: ExecutionSnapshot,
        candidate: ModelCandidate,
        model_input: ModelInput,
    ) -> RefreshResult:
        """모델 후보의 대상·revision·근거를 검증하여 결과를 결정할 경계.

        Args:
            snapshot: 판단의 기준이 되는 확정된 입력.
            candidate: 외부 모델이 반환한 미검증 후보와 사용량.
            model_input: 후보 생성에 사용한 프롬프트와 버전·모델 설정.

        Returns:
            구현 시 업무 검증을 마친 RefreshResult. 모델 후보를 그대로 신뢰하지 않는다.

        Raises:
            RulesNotImplemented: 후보 검증 규칙이 아직 구현되지 않아 항상 발생한다.
        """
        raise RulesNotImplemented("validate_candidate")
