"""실제 refresh 서비스와 평가 전용 IO를 조립한다. import 시 IO는 없다."""

from dataclasses import asdict, replace
from types import SimpleNamespace

from app.refresh.context import build_context
from app.refresh.errors import RefreshFailure
from app.refresh.models import (
    DocumentBatch,
    DocumentState,
    Failure,
    RelatedDocuments,
    RelationGenerationScope,
)
from app.refresh.rules import RefreshRules
from app.refresh.service import RefreshService
from app.refresh.source_alignment import align_candidate
from app.refresh.validation import relation_in_scope
from evaluation.cases import EvaluationCase, assess
from evaluation.confidence import observe_confidence
from evaluation.fakes import (
    Calls,
    FakeDocuments,
    FakeInbox,
    FakeJsonArtifacts,
    FakePublisher,
    FakeRelations,
)
from evaluation.repair_observation import observe_repair
from evaluation.source_observation import observe_sources


class CallBudget:
    """전체 사례·보완에 공유하는 실제 모델 시도 상한."""

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


def evaluation_snapshot(case: EvaluationCase):
    """case의 조회 자료로 순수 규칙 스냅샷을 구성한다. artifact 구현에 의존하지 않는다."""
    rules = RefreshRules()
    related = case.related if case.request.discover_related else RelatedDocuments()
    selection = rules.select(case.request, case.source, related)
    available = {
        document.document_id: document
        for document in case.targets
        if document.project_id == case.request.job.project_id
    }
    fetched = DocumentBatch(
        tuple(available[key] for key in selection.missing_ids if key in available),
        tuple(key for key in selection.missing_ids if key not in available),
    )
    return rules.assemble(case.request, case.source, related, selection, fetched)


def planned_calls(case: EvaluationCase, *, single_call=False) -> int:
    """신규 발견·대상 갱신 분리와 활성 입력을 실제 호출 계획으로 계산한다."""
    rules = EvaluationRules(None, None, single_call=single_call)
    snapshot = evaluation_snapshot(case)
    inputs = rules.model_inputs(snapshot, rules.chunk(snapshot))
    return (
        2
        if inputs
        and inputs[0].schema_version in {"refresh-candidate-v7", "refresh-candidate-v8"}
        else len(inputs)
    )


class EvaluationModel:
    """명시적으로 선택한 실제 모델 또는 고정 오프라인 후보를 호출한다."""

    def __init__(self, case, delegate, budget, recorder=None):
        self.case, self.delegate, self.budget = case, delegate, budget
        self.recorder = recorder
        self.snapshot = None

    def generate(self, model_input):
        # A rejected budget check never invokes the model and creates no attempt.
        self.budget.consume()
        if self.recorder is not None:
            return self.recorder.generate(
                model_input, lambda: self._generate(model_input)
            )
        return self._generate(model_input)

    def _generate(self, model_input):
        if self.delegate is not None:
            return self.delegate.generate(model_input)
        if self.snapshot is None:
            self.snapshot = evaluation_snapshot(self.case)
        snapshot = self.snapshot
        candidate = replace(
            self.case.expected,
            document_proposals=tuple(
                p
                for p in self.case.expected.document_proposals
                if model_input.allow_document_updates
                and p.target_document_id in model_input.target_ids
            ),
            new_document_proposals=(
                self.case.expected.new_document_proposals
                if model_input.allow_new_documents
                else ()
            ),
            relation_proposals=tuple(
                p
                for p in self.case.expected.relation_proposals
                if relation_in_scope(snapshot, p, model_input.relation_scope)
            ),
        )
        used = {
            key
            for p in candidate.document_proposals
            + candidate.relation_proposals
            + candidate.new_document_proposals
            for key in p.fact_ids
        }
        return replace(
            candidate, facts=tuple(f for f in candidate.facts if f.fact_id in used)
        )


class EvaluationRules(RefreshRules):
    """명시적인 과거 응답/기준선만 이전 계약으로 검사하며 live 계약은 완화하지 않는다."""

    def __init__(self, model, recorder, *, single_call=False):
        self.model, self.recorder = model, recorder
        self.single_call = single_call

    def model_inputs(self, snapshot, chunks):
        """평가 전용 단일 호출은 모든 대상과 신규 생성의 소유권을 함께 가진다."""
        if snapshot.request.model_settings.schema_version in {
            "refresh-candidate-v7",
            "refresh-candidate-v8",
        }:
            return super().model_inputs(snapshot, chunks)
        if not self.single_call:
            return super().model_inputs(snapshot, chunks)
        if snapshot.request.settings.separate_discovery:
            raise ValueError("Single-call evaluation requires combined discovery")
        if not any(
            document.state == DocumentState.ACTIVE
            and document.document_id in snapshot.changed_ids
            for document in snapshot.documents
        ):
            return ()
        return (
            self.build_prompt(
                build_context(
                    snapshot,
                    chunks,
                    relation_scope=RelationGenerationScope(snapshot.target_ids, True),
                )
            ),
        )

    def validate_plan(self, snapshot, plan, model_input):
        if self.recorder is not None:
            record = self.recorder.current
            record["stage"] = "PLAN"
            record["source_observation"] = {
                "status": "not_applicable",
                "reason": "planning_stage_has_no_facts",
            }
            record["confidence_observation"] = {
                source: {
                    "status": "not_applicable",
                    "reason": "planning_stage_has_no_facts",
                }
                for source in ("raw", "effective")
            }
            self.recorder.flush()
        return super().validate_plan(snapshot, plan, model_input)

    def validate_candidate(self, snapshot, candidate, model_input):
        legacy = None
        if self.model is None and not candidate.facts:
            legacy = "refresh-candidate-v3"  # old expected-fixture baseline only
        elif self.recorder is not None:
            version = self.recorder.current.get("replayed_schema_version")
            if version in {
                "refresh-candidate-v1",
                "refresh-candidate-v2",
                "refresh-candidate-v3",
                "refresh-candidate-v4",
            }:
                legacy = version
        if legacy is not None:
            model_input = replace(model_input, schema_version=legacy)
        candidate = align_candidate(snapshot, candidate, model_input)
        result = super().validate_candidate(snapshot, candidate, model_input)
        if self.recorder is not None and model_input.relation_scope is not None:
            raw = self.recorder.last_candidate
            if raw is not None:
                self.recorder.current["effective_candidate"] = asdict(candidate)
                if self.recorder.current["attempt_index"]:
                    self.recorder.current["repair_observation"] = observe_repair(
                        model_input, raw, candidate
                    )
                if self.recorder.observe_confidence:
                    self.recorder.current["confidence_observation"] = {
                        "raw": observe_confidence(
                            snapshot,
                            raw,
                            result.diagnostics
                            if asdict(raw) == asdict(candidate)
                            else None,
                        ),
                        "effective": observe_confidence(
                            snapshot, candidate, result.diagnostics
                        ),
                    }
        if (
            self.recorder is not None
            and self.recorder.observe_sources
            and model_input.relation_scope is not None
        ):
            self.recorder.current["source_observation"] = observe_sources(
                self.recorder.case, snapshot, candidate, model_input
            )
            raw = self.recorder.last_candidate
            if raw is not None and raw != candidate:
                self.recorder.current["raw_source_observation"] = observe_sources(
                    self.recorder.case, snapshot, raw, model_input
                )
            self.recorder.flush()
        elif self.recorder is not None:
            self.recorder.flush()
        return result


def assemble(
    case: EvaluationCase, model, budget: CallBudget, recorder=None, *, single_call=False
):
    """다섯 외부 IO를 fake로 고정하고 ProposalModel만 선택한다."""
    calls = Calls()
    artifacts = FakeJsonArtifacts({case.request.input_ref: case.source}, calls)
    service = RefreshService(
        artifacts=artifacts,
        relations=FakeRelations(case.related, calls),
        documents=FakeDocuments(case.targets, calls),
        model=EvaluationModel(case, model, budget, recorder),
        inbox=FakeInbox(calls=calls),
        publisher=FakePublisher(calls),
        rules=EvaluationRules(model, recorder, single_call=single_call),
    )
    return SimpleNamespace(
        service=service, artifacts=artifacts, calls=calls, recorder=recorder
    )


def run_case(case, scenario, *, repeat_saved=False):
    """한 사례를 실행하고 저장된 결과 복구를 선택적으로 검증한다."""
    completion = scenario.service.run(case.request)
    if repeat_saved:
        assert scenario.service.run(case.request) == completion
    result = scenario.artifacts.read_result(case.request)
    if getattr(scenario, "recorder", None) is not None:
        scenario.recorder.reconcile(result.attempts)
    assessment = assess(case, result)
    snapshot = scenario.artifacts.read_context(case.request)
    original = {
        d.document_id: d
        for d in case.source.documents + case.targets
        if d.folder_code == "MANUSCRIPT" and d.state == DocumentState.ACTIVE
    }
    saved = {d.document_id: d for d in snapshot.documents} if snapshot else {}
    source = scenario.artifacts.read_input(case.request)
    current_input = {d.document_id: d for d in source.documents}
    source_ids = {d.document_id for d in case.source.documents}
    assessment["manuscript_content_preserved"] = all(
        saved.get(key) == document
        and (key not in source_ids or current_input.get(key) == document)
        for key, document in original.items()
    ) and not any(p.target_document_id in original for p in result.document_proposals)
    return result, assessment
