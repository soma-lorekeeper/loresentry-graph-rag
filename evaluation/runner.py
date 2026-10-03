"""실제 refresh 서비스와 평가 전용 IO를 조립한다. import 시 IO는 없다."""

from dataclasses import replace
from types import SimpleNamespace

from app.refresh.errors import RefreshFailure
from app.refresh.models import Failure
from app.refresh.service import RefreshService
from evaluation.cases import EvaluationCase, assess
from evaluation.fakes import (
    Calls,
    FakeDocuments,
    FakeJobs,
    FakeJsonArtifacts,
    FakePublisher,
    FakeRelations,
)


class CallBudget:
    """전체 사례·재실행에 공유하는 호출 상한. 자동 재시도는 하지 않는다."""

    def __init__(self, maximum: int):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("Call budget must be positive")
        self.maximum = maximum
        self.used = 0

    def consume(self):
        if self.used >= self.maximum:
            raise RefreshFailure(
                Failure("EVALUATION_CALL_BUDGET", "Evaluation budget exhausted")
            )
        self.used += 1


class EvaluationModel:
    """명시적으로 선택한 실제 모델 또는 고정 오프라인 후보를 호출한다."""

    def __init__(self, case, delegate, budget):
        self.case, self.delegate, self.budget = case, delegate, budget

    def generate(self, model_input):
        self.budget.consume()
        if self.delegate is not None:
            return self.delegate.generate(model_input)
        return replace(
            self.case.expected,
            document_proposals=tuple(
                p
                for p in self.case.expected.document_proposals
                if p.target_document_id in model_input.target_ids
            ),
        )


def assemble(case: EvaluationCase, model, budget: CallBudget):
    """다섯 외부 IO를 fake로 고정하고 ProposalModel만 선택한다."""
    calls = Calls()
    artifacts = FakeJsonArtifacts({case.request.input_ref: case.source}, calls)
    service = RefreshService(
        artifacts=artifacts,
        relations=FakeRelations(case.related, calls),
        documents=FakeDocuments(case.targets, calls),
        model=EvaluationModel(case, model, budget),
        jobs=FakeJobs(calls=calls),
        publisher=FakePublisher(calls),
    )
    return SimpleNamespace(service=service, artifacts=artifacts, calls=calls)


def run_case(case, scenario, *, repeat_saved=False):
    """한 사례를 실행하고 저장된 결과 복구를 선택적으로 검증한다."""
    completion = scenario.service.run(case.request, "evaluation-1")
    if repeat_saved:
        assert scenario.service.run(case.request, "evaluation-2") == completion
    result = scenario.artifacts.read_result(case.request)
    return result, assess(case, result)
