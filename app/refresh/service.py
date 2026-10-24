"""조회·판단·실행을 조율하는 갱신안 생성 서비스.

외부 IO와 순수 업무 규칙을 주입한다. 실제 어댑터 선택과 클라이언트 수명은 조립 지점이 담당한다.
연동 오류 재시도와 파티션 실행 관리는 전달 계층이 담당한다. 현행 후보의 구체적
검증 오류는 같은 호출 범위에서 최대 두 번 보완한 뒤 결과를 저장한다.
"""

import json
from dataclasses import asdict, replace

from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.inbox import validate_completion
from app.refresh.models import (
    Completion,
    DocumentBatch,
    DocumentState,
    Failure,
    ModelAttempt,
    ModelCandidate,
    Outcome,
    Readiness,
    RefreshRequest,
    RefreshResult,
    RelatedDocuments,
    Usage,
)
from app.refresh.ports import (
    DocumentSource,
    ProposalModel,
    RefreshArtifactStore,
    RefreshInbox,
    RelatedDocumentSource,
    TokenCounter,
)
from app.refresh.repair import (
    MAX_REPAIRS_PER_CALL,
    add_usage,
    build_repair_input,
    failed_result,
    preserve_unaffected_proposals,
    proposal_counts,
    repairable,
)
from app.refresh.rules import RefreshRules


class RefreshService:
    """조회·모델·아티팩트·Inbox 포트와 판단 규칙으로 갱신안을 생성한다.

    판단은 rules에 위임하고 실행 스냅샷·결과·Inbox·Outbox 기록을 조율한다.
    문서 원문이나 관계 그래프에 갱신안을 적용하는 기능은 포함하지 않는다.
    """

    def __init__(
        self,
        *,
        artifacts: RefreshArtifactStore,
        relations: RelatedDocumentSource,
        documents: DocumentSource,
        model: ProposalModel,
        inbox: RefreshInbox,
        rules: RefreshRules | None = None,
        tokens: TokenCounter | None = None,
        plan_budget: int | None = None,
    ):
        """실행에 사용할 포트와 판단 규칙을 주입한다.

        Args:
            artifacts: 입력·실행 스냅샷·결과 저장소.
            relations: 관계 그래프 조회 구현.
            documents: 추가 본문 조회 구현.
            model: 모델 후보 생성 구현.
            inbox: 완료된 요청을 재처리하지 않기 위한 Inbox.
            rules: 순수 판단 구현. 생략하면 기본 순수 업무 규칙을 쓴다.
            tokens: 입력 토큰 계수. 주면 2단계 실행이 예산을 넘을 때 seed 묶음으로 나눈다.
            plan_budget: 계획 입력 하나에 허용하는 토큰. 작성·보완이 덧붙일 몫을 뺀 값이다.
        """
        if (tokens is None) != (plan_budget is None):
            raise ValueError("Token counter and plan budget are configured together")
        self.artifacts = artifacts
        self.relations = relations
        self.documents = documents
        self.model = model
        self.inbox = inbox
        self.rules = rules if rules is not None else RefreshRules()
        self.tokens = tokens
        self.plan_budget = plan_budget

    def run(self, request: RefreshRequest) -> Completion:
        """완료 Inbox를 확인하고 S3 결과·문맥을 재사용하거나 새로 실행한다.

        결과 저장 → Inbox와 발행 대기 Outbox의 원자적 기록 순서다. 반환 후에만
        전달 계층이 offset을 커밋한다. Kafka 발행은 별도 relay가 재시도한다.
        Inbox 이전 실패는 재전달하며 결과 저장 전 모델 호출은 반복될 수 있다.
        재시도 불가능한 계산 실패도 FAILED 결과와 발행 대기를 기록한다.
        """
        completed = self.inbox.find(request)
        if completed is not None:
            validate_completion(request, completed)
            return completed
        result = self.artifacts.read_result(request)
        if result is None:
            try:
                result = self._compute(request)
            except RefreshFailure as error:
                if error.failure.retryable:
                    raise
                result = RefreshResult(
                    request.job, Outcome.FAILED, failure=error.failure
                )
        return self._finish(request, result)

    def fail(self, request: RefreshRequest, failure: Failure) -> Completion:
        """실행하지 못한 요청(투영 대기 소진 등)을 FAILED로 끝내 완료를 한 번 기록한다.

        이미 완료됐거나 저장된 결과가 있으면 그것을 그대로 쓴다. 완료 이벤트 ID는 요청으로
        정해지고 결과는 한 번만 저장되므로 동시 실행과 경합해도 완료는 하나다.
        """
        completed = self.inbox.find(request)
        if completed is not None:
            validate_completion(request, completed)
            return completed
        result = self.artifacts.read_result(request) or RefreshResult(
            request.job,
            Outcome.FAILED,
            failure=failure,
            prompt_version=request.model_settings.prompt_version,
        )
        return self._finish(request, result)

    def _finish(self, request, result):
        """결과 저장 → Inbox와 발행 대기 기록. 먼저 저장된 결과가 있으면 그것을 쓴다."""
        if result.job != request.job:
            raise RequestConflict("result identity differs")
        try:
            result_ref = self.artifacts.save_result(request, result)
        except RequestConflict:
            # 동시 재실행의 먼저 저장된 결과를 사용한다. 다른 입력·설정은 저장소가 거절한다.
            saved = self.artifacts.read_result(request)
            if saved is None or saved.job != request.job:
                raise
            result = saved
            result_ref = self.artifacts.save_result(request, saved)
        completion = self._completion(request, result_ref, result)
        self.inbox.complete(request, completion)
        return completion

    def _compute(self, request):
        """실행 입력을 재사용하거나 조회·확정한 뒤 모델 후보를 검증한다.

        실행 스냅샷을 모델 호출 전에 저장한다. 저장소 조회 실패는 전파하며,
        아직 없는 경우에만 입력과 현재 관계·문서를 읽는다.
        """
        snapshot = self.artifacts.read_context(request)
        if snapshot is None:
            source = self.artifacts.read_input(request)
            self.rules.validate_input(request, source)
            related = RelatedDocuments()
            if request.discover_related:
                active_ids = {
                    document.document_id
                    for document in source.documents
                    if document.state == DocumentState.ACTIVE
                }
                discovery_request = replace(
                    request,
                    seed_ids=tuple(
                        seed for seed in request.seed_ids if seed in active_ids
                    ),
                )
                if discovery_request.seed_ids:
                    related = self.relations.find_related(discovery_request)
                if related.readiness != Readiness.READY:
                    raise RefreshFailure(
                        Failure(
                            "GRAPH_NOT_READY", "Relationship projection not ready", True
                        )
                    )
            selection = self.rules.select(request, source, related)
            fetched = DocumentBatch(())
            if selection.missing_ids:
                fetched = self.documents.fetch_documents(
                    request.job.project_id, selection.missing_ids
                )
            snapshot = self.rules.assemble(request, source, related, selection, fetched)
        if snapshot.request != request:
            raise RequestConflict("execution snapshot belongs to a different request")
        try:
            self.artifacts.save_context(snapshot)
        except RequestConflict:
            saved = self.artifacts.read_context(request)
            if saved is None or saved.request != request:
                raise
            snapshot = saved
        chunks = self.rules.chunk(snapshot)
        inputs = self.rules.model_inputs(snapshot, chunks)
        if len(inputs) > request.settings.max_model_calls:
            raise RefreshFailure(
                Failure("CALL_BUDGET_EXCEEDED", "Model call budget exceeded")
            )
        if not inputs:
            return RefreshResult(
                request.job,
                Outcome.NO_CHANGE,
                prompt_version=request.model_settings.prompt_version,
            )
        usage = Usage()
        attempts = []
        from app.refresh.workflow import WRITE_SCHEMAS

        if len(inputs) == 1 and inputs[0].schema_version in WRITE_SCHEMAS:
            if self.tokens is None:
                return self._two_stage(snapshot, inputs[0])
            return self._batched(snapshot, inputs[0])
        documents, relations, new_documents = [], [], []
        facts = {}
        for index, model_input in enumerate(inputs):
            candidate, checked, usage = self._validated_call(
                snapshot,
                model_input,
                index + 1,
                len(inputs) - index - 1,
                usage,
                attempts,
            )
            if checked.outcome == Outcome.FAILED:
                return failed_result(checked, checked.failure, usage, attempts)
            if len(inputs) == 1:
                return replace(checked, usage=usage, attempts=tuple(attempts))
            # Keep raw proposals until aggregate validation so conflicts with no-ops
            # or candidates from another call cannot disappear during normalization.
            documents.extend(candidate.document_proposals)
            relations.extend(candidate.relation_proposals)
            new_documents.extend(candidate.new_document_proposals)
            referenced = {
                key
                for proposal in candidate.document_proposals
                + candidate.relation_proposals
                + candidate.new_document_proposals
                for key in proposal.fact_ids
            }
            for fact in candidate.facts:
                if fact.fact_id not in referenced:
                    continue
                previous = facts.get(fact.fact_id)
                if previous is not None and previous != fact:
                    return RefreshResult(
                        request.job,
                        Outcome.FAILED,
                        usage=usage,
                        failure=Failure(
                            "INVALID_FACT", "Conflicting aggregate fact identifier"
                        ),
                        prompt_version=model_input.prompt_version,
                        candidate_schema_version=model_input.schema_version,
                        attempts=tuple(attempts),
                    )
                facts[fact.fact_id] = fact
        aggregate = ModelCandidate(
            tuple(documents),
            usage,
            tuple(relations),
            tuple(new_documents),
            tuple(facts[key] for key in sorted(facts)),
        )
        result = self.rules.validate_candidate(
            snapshot,
            aggregate,
            replace(
                inputs[0],
                target_ids=snapshot.target_ids,
                relation_scope=None,
                allow_document_updates=True,
            ),
        )
        return replace(result, usage=usage, attempts=tuple(attempts))

    def _batched(self, snapshot, base):
        """계획 입력이 예산을 넘으면 seed 묶음을 차례로 실행하고 누적 결과를 한 번 검증한다.

        모든 seed가 한 번에 들어가면 지금과 같은 2단계 실행 하나로 끝낸다. 끝난 묶음은 저장해
        재전달된 요청이 다음 묶음부터 이어 가게 한다. 한 묶음이라도 실패하면 요청 전체가 실패다.
        """
        from app.refresh.batching import (
            BatchState,
            aggregate,
            batch_snapshot,
            failed,
            fold,
        )

        state, remaining, index = BatchState(), snapshot.changed_ids, 1
        while remaining:
            saved = self.artifacts.read_batch(snapshot.request, index)
            if saved is None:
                seeds = self._pack(snapshot, state, remaining)
                if index == 1 and seeds == remaining:
                    return self._two_stage(snapshot, base)
                batch = batch_snapshot(snapshot, state, seeds)
                result = self._two_stage(batch, self._batch_input(batch))
                if result.outcome == Outcome.FAILED:
                    return failed(state, result)
                try:
                    self.artifacts.save_batch(snapshot.request, index, seeds, result)
                except RequestConflict:
                    # 동시 재실행이 먼저 저장한 묶음을 쓴다.
                    saved = self.artifacts.read_batch(snapshot.request, index)
                    if saved is None:
                        raise
            if saved is not None:
                seeds, result = saved
                if seeds != remaining[: len(seeds)]:
                    raise RequestConflict("Saved batch differs from the seed order")
            try:
                fold(state, result)
            except RefreshFailure as error:
                return failed(state, result, error.failure)
            remaining, index = remaining[len(seeds) :], index + 1
        return aggregate(snapshot, state, base)

    def _batch_input(self, batch):
        """묶음 스냅샷의 2단계 기본 입력. 전체 스냅샷과 같은 규칙으로 만든다."""
        return self.rules.model_inputs(batch, self.rules.chunk(batch))[0]

    def _pack(self, snapshot, state, remaining):
        """계획 입력이 예산에 드는 가장 긴 seed 앞부분을 count API 이분 탐색으로 고른다.

        문맥은 문서 수에 비례해 늘지 않으므로(관계 쌍 목록 등) 앞부분마다 실제 계획 입력을
        만들어 센다. 남은 seed가 전부 들면 count 한 번으로 끝나고, 아니면 묶음마다
        log2(남은 seed 수)번쯤 더 센다. seed 하나와 그 이웃만으로도 넘으면 이웃을 빼지 않고
        실패시킨다.
        """
        from app.refresh.batching import batch_snapshot
        from app.refresh.workflow import build_plan_input

        def tokens(count):
            # 앞에서 count개 seed로 묶음을 만들었을 때 계획 입력의 실제 토큰 수
            batch = batch_snapshot(snapshot, state, remaining[:count])
            return self.tokens.count(build_plan_input(batch, self._batch_input(batch)))

        if tokens(len(remaining)) <= self.plan_budget:
            return remaining
        # fits개는 예산에 들고 too_big개는 넘는다는 것을 유지하며 범위를 좁힌다.
        fits, too_big = 0, len(remaining)
        while too_big - fits > 1:
            middle = (fits + too_big) // 2
            if tokens(middle) <= self.plan_budget:
                fits = middle
            else:
                too_big = middle
        if not fits:
            raise RefreshFailure(
                Failure(
                    "CONTEXT_BUDGET_EXCEEDED",
                    "A changed document and its related documents exceed the model "
                    "input budget",
                )
            )
        return remaining[:fits]

    def _two_stage(self, snapshot, base):
        """계획·작성 두 호출과 전체 한 번의 보완. 계획 누락을 작성 단계에서 허용하지 않는다."""
        from app.refresh.workflow import (
            build_plan_input,
            build_write_input,
            plan_payload,
        )

        current = build_plan_input(snapshot, base)
        initial = current
        usage, attempts = Usage(), []
        plan = None
        for attempt_index in range(2):
            try:
                plan = self.model.generate(current)
                plan = self.rules.validate_plan(snapshot, plan, current)
            except RefreshFailure as error:
                observed = Usage(1, error.usage.input_tokens, error.usage.output_tokens)
                if plan is not None:
                    observed = Usage(
                        1, plan.usage.input_tokens, plan.usage.output_tokens
                    )
                failure, diagnostics = error.failure, error.diagnostics
                previous = (
                    plan_payload(plan) if plan is not None else error.candidate_text
                )
                plan = None
            else:
                observed = Usage(1, plan.usage.input_tokens, plan.usage.output_tokens)
                failure, diagnostics = None, ()
            usage = add_usage(usage, observed)
            attempts.append(
                ModelAttempt(
                    1,
                    attempt_index,
                    base.target_ids,
                    Outcome.FAILED if failure else Outcome.PROPOSED,
                    observed,
                    failure=failure,
                    diagnostics=diagnostics,
                    stage="PLAN",
                )
            )
            if failure is None:
                break
            if (
                attempt_index
                or failure.retryable
                or failure.code not in {"INVALID_CHANGE_PLAN", "MODEL_INVALID_RESPONSE"}
                or usage.calls + 2 > snapshot.request.settings.max_model_calls
            ):
                return RefreshResult(
                    snapshot.request.job,
                    Outcome.FAILED,
                    usage=usage,
                    failure=failure,
                    prompt_version=base.prompt_version,
                    candidate_schema_version=base.schema_version,
                    attempts=tuple(attempts),
                    diagnostics=diagnostics,
                )
            context = json.loads(initial.prompt)
            context["validation_repair"] = {
                "version": "refresh-plan-repair-v1",
                "attempt": 1,
                "previous_candidate": previous,
                "diagnostics": [asdict(d) for d in diagnostics],
            }
            # 보완 입력도 모델 어댑터가 호출 직전 토큰 예산으로 검사한다.
            prompt = json.dumps(context, ensure_ascii=False)
            current = replace(
                initial,
                prompt=prompt,
                instructions=initial.instructions
                + "\n진단된 계획의 범위·ID·근거를 수정하고 전체 계획 JSON을 반환한다.",
            )
        try:
            write_input = build_write_input(snapshot, base, plan)
        except RefreshFailure as error:
            return RefreshResult(
                snapshot.request.job,
                Outcome.FAILED,
                usage=usage,
                failure=error.failure,
                prompt_version=base.prompt_version,
                candidate_schema_version=base.schema_version,
                attempts=tuple(attempts),
                change_plan=plan,
            )
        _, checked, usage = self._validated_call(
            snapshot,
            write_input,
            2,
            0,
            usage,
            attempts,
            max_repairs=1 - (len(attempts) - 1),
        )
        return replace(checked, usage=usage, attempts=tuple(attempts), change_plan=plan)

    def _validated_call(
        self,
        snapshot,
        model_input,
        call_index,
        remaining_calls,
        usage,
        attempts,
        *,
        max_repairs=MAX_REPAIRS_PER_CALL,
    ):
        """하나의 후보를 검증하고 진단이 있는 오류만 최대 두 번 보완한다.

        보완은 이미 통과한 다른 호출을 반복하지 않는다. 남은 최초 호출 예산을 예약하고
        사실 표현 오류의 무관한 제안은 이전 후보에서 보존하고 전체를 재검증한다.
        최종 통합 실패와 연동 오류는 이 루프의 대상이 아니다.
        """
        current_input = model_input
        previous_candidate, previous_diagnostics = None, ()
        for attempt_index in range(max_repairs + 1):
            try:
                candidate = self.model.generate(current_input)
            except RefreshFailure as error:
                if error.failure.retryable:
                    raise
                candidate = error.candidate_text
                checked = RefreshResult(
                    snapshot.request.job,
                    Outcome.FAILED,
                    usage=error.usage,
                    failure=error.failure,
                    prompt_version=model_input.prompt_version,
                    candidate_schema_version=(
                        model_input.schema_version
                        if model_input.schema_version
                        in {
                            "refresh-candidate-v5",
                            "refresh-candidate-v6",
                            "refresh-candidate-v7",
                            "refresh-candidate-v8",
                        }
                        else None
                    ),
                    diagnostics=error.diagnostics,
                )
                if error.failure.code == "EVALUATION_CALL_BUDGET" or (
                    error.failure.code == "MODEL_REQUEST_INVALID"
                    and error.usage.calls == 0
                ):
                    # The evaluator/adapter rejected the invocation before IO.
                    return None, checked, usage
            else:
                if previous_candidate is not None:
                    candidate = preserve_unaffected_proposals(
                        previous_candidate, candidate, previous_diagnostics
                    )
                if model_input.schema_version in {
                    "refresh-candidate-v7",
                    "refresh-candidate-v8",
                } and hasattr(candidate, "paragraph_edits"):
                    from app.refresh.workflow import collect_source_extensions

                    candidate = replace(
                        candidate,
                        source_scope_extensions=collect_source_extensions(
                            candidate, current_input
                        ),
                    )
                checked = self.rules.validate_candidate(
                    snapshot, candidate, current_input
                )
            observed = Usage(1, checked.usage.input_tokens, checked.usage.output_tokens)
            usage = add_usage(usage, observed)
            attempts.append(
                ModelAttempt(
                    call_index,
                    attempt_index,
                    model_input.target_ids,
                    checked.outcome,
                    observed,
                    **proposal_counts(candidate),
                    failure=checked.failure,
                    diagnostics=checked.diagnostics,
                    stage="WRITE"
                    if model_input.schema_version
                    in {"refresh-candidate-v7", "refresh-candidate-v8"}
                    else "GENERATE",
                )
            )
            if checked.outcome != Outcome.FAILED:
                return candidate, checked, usage
            if attempt_index == max_repairs or not repairable(
                model_input, checked.failure, checked.diagnostics
            ):
                return None, checked, usage
            if usage.calls + 1 + remaining_calls > (
                snapshot.request.settings.max_model_calls
            ):
                checked = replace(
                    checked,
                    failure=Failure(
                        "CALL_BUDGET_EXCEEDED",
                        "No repair call available after reserving remaining calls",
                    ),
                )
                return None, checked, usage
            previous_candidate, previous_diagnostics = candidate, checked.diagnostics
            current_input = build_repair_input(
                model_input,
                candidate,
                checked.diagnostics,
                attempt=attempt_index + 1,
            )

    @staticmethod
    def _completion(request, result_ref, result):
        """요청과 결과 식별이 같을 때 완료 값을 만들고, 다르면 RequestConflict를 발생시킨다."""
        if result.job != request.job:
            raise RequestConflict("result identity differs")
        return Completion(
            request.job,
            result_ref,
            result.outcome,
            result.failure,
            result.prompt_version,
        )
