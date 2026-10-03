"""조회·판단·실행을 조율하는 갱신안 생성 골격.

외부 IO는 주입하며 업무 규칙은 미구현 상태다. HTTP·Kafka에 연결하지 않았다.
실행 ID, 재시도 횟수, 임대 복구는 전달 계층이 담당하고 여기서는 자동 재시도하지 않는다.
"""

from dataclasses import replace

from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import (
    Completion, DocumentBatch, Failure, JobState, Outcome, Readiness,
    RefreshRequest, RefreshResult, RelatedDocuments,
)
from app.refresh.ports import (
    DocumentSource, ProposalModel, RefreshArtifactStore, RefreshCompletionPublisher,
    RefreshJobStore, RelatedDocumentSource,
)
from app.refresh.rules import RefreshRules


class RefreshService:
    """여섯 IO 포트와 판단 규칙을 연결하여 갱신안 생성 작업을 진행한다.

    판단은 rules에 위임하고 실행 스냅샷·결과·작업 상태 저장과 완료 발행을 조율한다.
    문서 원문이나 관계 그래프에 갱신안을 적용하는 기능은 포함하지 않는다.
    """
    def __init__(self, *, artifacts: RefreshArtifactStore, relations: RelatedDocumentSource,
                 documents: DocumentSource, model: ProposalModel, jobs: RefreshJobStore,
                 publisher: RefreshCompletionPublisher, rules: RefreshRules | None = None):
        """실행에 사용할 포트와 판단 규칙을 주입한다.

        Args:
            artifacts: 입력·실행 스냅샷·결과 저장소.
            relations: 관계 그래프 조회 구현.
            documents: 추가 본문 조회 구현.
            model: 모델 후보 생성 구현.
            jobs: 실행 소유권과 복구 지점 저장소.
            publisher: 완료 메시지 발행 구현.
            rules: 순수 판단 구현. 생략하면 미구현 예외를 발생시키는 기본 골격을 쓴다.
        """
        self.artifacts = artifacts
        self.relations = relations
        self.documents = documents
        self.model = model
        self.jobs = jobs
        self.publisher = publisher
        self.rules = rules if rules is not None else RefreshRules()

    def run(self, request: RefreshRequest, execution_id: str) -> Completion:
        # A rejected claim grants no ownership, so it must not release another job.
        """작업을 한 번 실행하거나 저장된 결과에서 완료 처리를 재개한다.

        Args:
            request: 입력 위치, 대상, 탐색 설정을 포함한 요청.
            execution_id: 전달 계층이 부여한 이번 실행의 소유권 식별자.

        Returns:
            저장된 결과의 위치와 처리 결과. 이미 완료된 요청은 다시 발행하지 않는다.
            계산 중 발생한 재시도 불가능한 RefreshFailure는 FAILED 결과로 저장·발행한다.

        Raises:
            JobBusy: 다른 실행이 소유권을 보유한 경우.
            RequestConflict: 요청·결과 식별 또는 실행 소유권이 충돌한 경우.
            RefreshFailure: 재시도 가능한 계산 실패 또는 저장·발행 등 계산 밖의 실패.
            RulesNotImplemented: 주입된 판단 규칙이 구현되지 않은 경우.

        예상하지 못한 예외도 그대로 전파한다. 실패 시 소유 중인 작업을 RETRYABLE로
        전환하려 시도하지만, 이는 전달 계층의 무조건적인 재시도를 뜻하지 않는다.
        저장된 결과는 재사용한다. 모델 응답 뒤 결과 저장 전에 실패하면 모델을 다시
        호출할 수 있고, 발행 뒤 완료 기록 전에 실패하면 메시지를 중복 발행할 수 있다.
        여러 저장소와 메시지 발행을 묶는 단일 트랜잭션은 제공하지 않는다.
        """
        record = self.jobs.claim(request, execution_id)
        try:
            result = self.artifacts.read_result(request)
            if record.state == JobState.COMPLETED:
                if result is None or record.result_ref is None:
                    raise RefreshFailure(Failure("RESULT_MISSING", "Completed result unavailable"))
                return self._completion(request, record.result_ref, result)
            if record.result_ref is not None and result is None:
                raise RefreshFailure(Failure("RESULT_MISSING", "Saved result unavailable"))
            if result is None:
                try:
                    result = self._compute(request, record)
                except RefreshFailure as error:
                    if error.failure.retryable:
                        raise
                    result = RefreshResult(request.job, Outcome.FAILED, failure=error.failure)
            # Idempotent write also recovers a result saved before a job checkpoint.
            if result.job != request.job:
                raise RequestConflict("result identity differs")
            result_ref = self.artifacts.save_result(request, result)
            # _compute may have checkpointed a context reference.
            current = self.jobs.load(request)
            if current is None or current.execution_id != execution_id:
                raise RequestConflict("job owner changed during execution")
            record = replace(current, state=JobState.RESULT_READY, result_ref=result_ref,
                             failure=result.failure)
            self.jobs.checkpoint(record)
            completion = self._completion(request, result_ref, result)
            self.publisher.publish(completion)
            self.jobs.checkpoint(replace(record, state=JobState.COMPLETED))
            return completion
        except Exception as error:
            # Release known ownership only. A failed release remains visible for
            # delivery/lease recovery; it must not mask the original failure.
            try:
                current = self.jobs.load(request)
                if (current is not None and current.execution_id == execution_id
                        and current.state not in {JobState.COMPLETED, JobState.RETRYABLE}):
                    failure = error.failure if isinstance(error, RefreshFailure) else None
                    self.jobs.checkpoint(replace(current, state=JobState.RETRYABLE, failure=failure))
            except Exception as release_error:
                error.add_note(f"Job release failed: {type(release_error).__name__}")
            raise

    def _compute(self, request, record):
        """실행 입력을 재사용하거나 조회·확정한 뒤 모델 후보를 검증한다.

        실행 스냅샷을 모델 호출 전에 저장한다. 복구 참조가 있는데 원본을 읽지 못하면
        새 조회로 대체하지 않고 실패한다. 이 메서드의 IO는 주입된 포트가 수행한다.
        """
        snapshot = self.artifacts.read_context(request)
        if snapshot is None:
            if record.context_ref is not None:
                raise RefreshFailure(Failure("CONTEXT_MISSING", "Saved execution snapshot unavailable"))
            source = self.artifacts.read_input(request)
            self.rules.validate_input(request, source)
            related = RelatedDocuments()
            if request.discover_related:
                related = self.relations.find_related(request)
                if related.readiness != Readiness.READY:
                    raise RefreshFailure(Failure("GRAPH_NOT_READY", "Relationship projection not ready", True))
            selection = self.rules.select(request, source, related)
            fetched = DocumentBatch(())
            if selection.missing_ids:
                fetched = self.documents.fetch_documents(request.job.project_id, selection.missing_ids)
            snapshot = self.rules.assemble(request, source, related, selection, fetched)
        if snapshot.request != request:
            raise RequestConflict("execution snapshot belongs to a different request")
        context_ref = self.artifacts.save_context(snapshot)
        self.jobs.checkpoint(replace(record, context_ref=context_ref))
        chunks = self.rules.chunk(snapshot)
        context = self.rules.build_context(snapshot, chunks)
        model_input = self.rules.build_prompt(context)
        candidate = self.model.generate(model_input)
        return self.rules.validate_candidate(snapshot, candidate, model_input)

    @staticmethod
    def _completion(request, result_ref, result):
        """요청과 결과 식별이 같을 때 완료 값을 만들고, 다르면 RequestConflict를 발생시킨다."""
        if result.job != request.job:
            raise RequestConflict("result identity differs")
        return Completion(request.job, result_ref, result.outcome, result.failure, result.prompt_version)
