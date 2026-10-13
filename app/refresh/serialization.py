"""S3 결과와 완료 이벤트의 순수 JSON 변환. 실제 저장·발행 및 재시도는 수행하지 않는다."""

from dataclasses import asdict

from app.refresh.evidence import unique_evidence
from app.refresh.facts import fact_description
from app.refresh.models import (
    Completion,
    DocumentProposal,
    Evidence,
    FactEntity,
    Failure,
    JobKey,
    ModelAttempt,
    NewDocumentProposal,
    Outcome,
    RefreshResult,
    RelationProposal,
    SourceFact,
    Usage,
    ValidationDiagnostic,
)

RESULT_SCHEMA_VERSION = "refresh-result-v3"
_CANDIDATE_SCHEMAS = {
    "refresh-candidate-v1",
    "refresh-candidate-v2",
    "refresh-candidate-v3",
    "refresh-candidate-v4",
    "refresh-candidate-v5",
}


def result_to_payload(request, result, snapshot=None):
    """결과를 출처·근거 사전과 제안 참조로 직렬화한다. 성공 결과에는 스냅샷이 필요하다."""
    if result.job != request.job or (
        snapshot is not None and snapshot.request != request
    ):
        raise ValueError("Result or snapshot identity differs")
    if result.outcome != Outcome.FAILED and snapshot is None:
        raise ValueError("Successful result requires execution snapshot")
    _check_outcome(result)
    evidence = unique_evidence(
        e
        for p in result.facts
        + result.document_proposals
        + result.relation_proposals
        + result.new_document_proposals
        for e in p.evidence
    )
    refs = {e: f"E{i:04d}" for i, e in enumerate(evidence, 1)}

    def proposal_payload(proposal):
        value = asdict(proposal)
        value.pop("evidence")
        value["evidence_refs"] = [refs[e] for e in proposal.evidence]
        return value

    changed_ids = snapshot.changed_ids if snapshot is not None else ()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "request_id": result.job.request_id,
        "project_id": result.job.project_id,
        "outcome": result.outcome.value,
        "document_proposals": [proposal_payload(p) for p in result.document_proposals],
        "relation_proposals": [proposal_payload(p) for p in result.relation_proposals],
        "new_document_proposals": [
            proposal_payload(p) for p in result.new_document_proposals
        ],
        "facts": [proposal_payload(fact) for fact in result.facts],
        "sources": [
            {
                "document_id": d.document_id,
                "revision_no": d.revision_no,
                "origin": "INPUT" if d.document_id in changed_ids else "CONTENT",
                "state": d.state.value,
            }
            for d in snapshot.documents
        ]
        if snapshot is not None
        else [],
        "evidence": [{"evidence_id": refs[e], **asdict(e)} for e in evidence],
        "error": asdict(result.failure) if result.failure else None,
        "execution": {
            "input_fingerprint": request.job.input_fingerprint,
            "model": request.model_settings.model,
            "prompt_version": result.prompt_version,
            "schema_version": request.model_settings.schema_version,
            **(
                {"candidate_schema_version": result.candidate_schema_version}
                if result.candidate_schema_version is not None
                else {}
            ),
            "settings": {
                key: value
                for key, value in asdict(request.settings).items()
                if (key != "separate_discovery" or value is not False)
                and (key != "chunk_overlap_chars" or value != 0)
            },
            "usage": asdict(result.usage),
            **(
                {"attempts": [asdict(a) for a in result.attempts]}
                if result.attempts
                else {}
            ),
            **(
                {"validation_diagnostics": [asdict(d) for d in result.diagnostics]}
                if result.diagnostics
                else {}
            ),
            "changed_ids": list(changed_ids),
            "target_ids": list(snapshot.target_ids) if snapshot else [],
            "observed_graph_version": snapshot.related.observed_version
            if snapshot
            else None,
        },
    }


def result_from_payload(payload):
    """저장된 v1~v3 결과를 복원한다. 소비자별 메타데이터는 원본 JSON에 보존한다.

    외부 모델 응답 파서가 아니다. 저장 객체의 무결성·프로젝트 권한 검사는 어댑터가
    수행한다. 알려진 필드와 결과 상태·근거 참조가 깨졌으면 ValueError를 발생시킨다.
    """
    try:
        if payload["schema_version"] not in {
            RESULT_SCHEMA_VERSION,
            "refresh-result-v2",
            "refresh-result-v1",
        }:
            raise ValueError("Unsupported result schema")
        evidence = {}
        for raw in payload["evidence"]:
            item = dict(raw)
            key = item.pop("evidence_id")
            if key in evidence:
                raise ValueError("Duplicate evidence reference")
            evidence[key] = Evidence(**item)

        def proposals(values, cls):
            result = []
            for raw in values:
                item = dict(raw)
                if (
                    payload["schema_version"] == RESULT_SCHEMA_VERSION
                    and cls is not SourceFact
                ):
                    refs = item.get("fact_ids")
                    if (
                        not isinstance(refs, list)
                        or not all(isinstance(key, str) for key in refs)
                        or len(refs) != len(set(refs))
                    ):
                        raise ValueError("Stored proposal requires unique fact_ids")
                item["evidence"] = tuple(
                    evidence[key] for key in item.pop("evidence_refs")
                )
                if "fact_ids" in item:
                    item["fact_ids"] = tuple(item["fact_ids"])
                if cls is SourceFact:
                    for role in ("actor", "recipient", "object"):
                        if item[role] is not None:
                            item[role] = FactEntity(**item[role])
                result.append(cls(**item))
            return tuple(result)

        execution = payload["execution"]
        if "candidate_schema_version" in execution and (
            not isinstance(execution["candidate_schema_version"], str)
            or execution["candidate_schema_version"] not in _CANDIDATE_SCHEMAS
        ):
            raise ValueError("Unsupported stored candidate schema")
        result = RefreshResult(
            JobKey(
                payload["project_id"],
                payload["request_id"],
                execution["input_fingerprint"],
            ),
            Outcome(payload["outcome"]),
            proposals(payload["document_proposals"], DocumentProposal),
            Usage(**execution["usage"]),
            Failure(**payload["error"]) if payload["error"] is not None else None,
            execution["prompt_version"],
            proposals(payload["relation_proposals"], RelationProposal),
            proposals(
                payload["new_document_proposals"]
                if payload["schema_version"] != "refresh-result-v1"
                else [],
                NewDocumentProposal,
            ),
            proposals(payload["facts"], SourceFact)
            if payload["schema_version"] == RESULT_SCHEMA_VERSION
            else (),
            execution.get("candidate_schema_version"),
            tuple(
                _attempt_from_payload(item) for item in execution.get("attempts", [])
            ),
            tuple(
                ValidationDiagnostic(**item)
                for item in execution.get("validation_diagnostics", [])
            ),
        )
        _check_outcome(result)
        if (
            payload["schema_version"] == RESULT_SCHEMA_VERSION
            and execution.get("candidate_schema_version", execution["schema_version"])
            in {"refresh-candidate-v4", "refresh-candidate-v5"}
            and any(
                not p.fact_ids
                for p in result.document_proposals
                + result.relation_proposals
                + result.new_document_proposals
            )
        ):
            raise ValueError("Current stored proposals require structured facts")
        return result
    except (KeyError, TypeError) as error:
        raise ValueError("Malformed stored result") from error


def _check_outcome(result):
    _check_attempts(result)
    if result.candidate_schema_version is not None and (
        not isinstance(result.candidate_schema_version, str)
        or result.candidate_schema_version not in _CANDIDATE_SCHEMAS
    ):
        raise ValueError("Unsupported stored candidate schema")
    facts = {fact.fact_id: fact for fact in result.facts}
    if len(facts) != len(result.facts):
        raise ValueError("Duplicate stored fact identifier")
    used = set()
    for proposal in (
        result.document_proposals
        + result.new_document_proposals
        + result.relation_proposals
    ):
        if (
            result.candidate_schema_version
            in {"refresh-candidate-v4", "refresh-candidate-v5"}
            and not proposal.fact_ids
        ):
            raise ValueError("Current stored proposals require structured facts")
        if any(key not in facts for key in proposal.fact_ids):
            raise ValueError("Unknown stored fact reference")
        used.update(proposal.fact_ids)
    if used != set(facts):
        raise ValueError("Unreferenced stored fact")
    for proposal in result.relation_proposals:
        if proposal.fact_ids and (
            proposal.description
            != fact_description(facts[key] for key in proposal.fact_ids)
            or unique_evidence(proposal.evidence)
            != unique_evidence(
                e for key in proposal.fact_ids for e in facts[key].evidence
            )
        ):
            raise ValueError("Stored relation differs from facts")
    has_proposals = bool(
        result.document_proposals
        or result.relation_proposals
        or result.new_document_proposals
    )
    if result.outcome == Outcome.FAILED:
        if has_proposals or result.facts or result.failure is None:
            raise ValueError("Failed result must have error and no proposals")
    elif (
        result.failure is not None
        or (result.outcome == Outcome.PROPOSED) != has_proposals
    ):
        raise ValueError("Result outcome conflicts with proposals or error")


def _attempt_from_payload(raw):
    """보완 이력의 열거형·불변 값과 내부 진단을 복원한다."""
    item = dict(raw)
    item["target_ids"] = tuple(item["target_ids"])
    item["outcome"] = Outcome(item["outcome"])
    item["usage"] = Usage(**item["usage"])
    item["failure"] = Failure(**item["failure"]) if item["failure"] else None
    item["diagnostics"] = tuple(
        ValidationDiagnostic(**diagnostic) for diagnostic in item["diagnostics"]
    )
    return ModelAttempt(**item)


def _check_diagnostics(diagnostics):
    if not isinstance(diagnostics, tuple) or any(
        not isinstance(d, ValidationDiagnostic)
        or not isinstance(d.path, str)
        or not d.path
        or not isinstance(d.rule, str)
        or not d.rule
        or any(
            value is not None and not isinstance(value, str)
            for value in (d.fact_id, d.source_marker, d.offending_value)
        )
        for d in diagnostics
    ):
        raise ValueError("Malformed validation diagnostics")


def _check_attempts(result):
    """시도 순서·상태·관측량을 검증한다. 이력이 없는 과거 결과도 읽는다."""
    _check_diagnostics(result.diagnostics)
    if not isinstance(result.attempts, tuple):
        raise ValueError("Attempt tuple required")
    previous = None
    for attempt in result.attempts:
        if (
            not isinstance(attempt, ModelAttempt)
            or type(attempt.call_index) is not int
            or attempt.call_index < 1
            or type(attempt.attempt_index) is not int
            or attempt.attempt_index not in {0, 1}
            or not isinstance(attempt.target_ids, tuple)
            or not all(
                isinstance(target, str) and target for target in attempt.target_ids
            )
            or len(set(attempt.target_ids)) != len(attempt.target_ids)
            or not isinstance(attempt.outcome, Outcome)
            or not isinstance(attempt.usage, Usage)
            or attempt.usage.calls != 1
            or any(
                type(value) is not int or value < 0
                for value in (
                    attempt.usage.calls,
                    attempt.usage.input_tokens,
                    attempt.usage.output_tokens,
                )
            )
            or any(
                value is not None and (type(value) is not int or value < 0)
                for value in (
                    attempt.document_proposals,
                    attempt.relation_proposals,
                    attempt.new_document_proposals,
                )
            )
            or (attempt.outcome == Outcome.FAILED) != (attempt.failure is not None)
            or (
                attempt.failure is not None and not isinstance(attempt.failure, Failure)
            )
        ):
            raise ValueError("Malformed model attempt")
        _check_diagnostics(attempt.diagnostics)
        if previous is None:
            valid_order = attempt.call_index == 1 and attempt.attempt_index == 0
        elif attempt.call_index == previous.call_index:
            valid_order = (
                previous.attempt_index == 0
                and previous.outcome == Outcome.FAILED
                and attempt.attempt_index == 1
                and attempt.target_ids == previous.target_ids
            )
        else:
            valid_order = (
                attempt.call_index == previous.call_index + 1
                and attempt.attempt_index == 0
                and previous.outcome != Outcome.FAILED
            )
        if not valid_order:
            raise ValueError("Model attempt sequence invalid")
        previous = attempt
    if (
        previous is not None
        and previous.outcome == Outcome.FAILED
        and result.outcome != Outcome.FAILED
    ):
        raise ValueError("Successful result ends with failed model attempt")
    if result.attempts and result.usage != Usage(
        len(result.attempts),
        sum(a.usage.input_tokens for a in result.attempts),
        sum(a.usage.output_tokens for a in result.attempts),
    ):
        raise ValueError("Attempt usage differs from result")


def completion_to_payload(completion: Completion):
    """내부 PROPOSED·NO_CHANGE를 SUCCEEDED로 매핑하며 Kafka에는 위치만 넣는다."""
    succeeded = completion.outcome in {Outcome.PROPOSED, Outcome.NO_CHANGE}
    if succeeded and (completion.result_ref is None or completion.failure is not None):
        raise ValueError("Success requires stored result and no error")
    if not succeeded and completion.failure is None:
        raise ValueError("Failure requires error")
    return {
        "request_id": completion.job.request_id,
        "project_id": completion.job.project_id,
        "outcome": "SUCCEEDED" if succeeded else "FAILED",
        "result": asdict(completion.result_ref) if completion.result_ref else None,
        "error": {
            "code": completion.failure.code,
            "message": completion.failure.message,
        }
        if completion.failure
        else None,
        "prompt_version": completion.prompt_version,
    }


def storage_failure_completion(request, failure):
    """전달 계층이 재시도 소진 뒤 사용할 저장 불가 실패 값. 발행 여부는 호출자가 결정한다."""
    return Completion(
        request.job,
        None,
        Outcome.FAILED,
        failure,
        request.model_settings.prompt_version,
    )
