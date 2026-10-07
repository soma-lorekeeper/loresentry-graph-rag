"""S3 결과와 완료 이벤트의 순수 JSON 변환. 실제 저장·발행 및 재시도는 수행하지 않는다."""

from dataclasses import asdict

from app.refresh.evidence import unique_evidence
from app.refresh.models import (
    Completion,
    DocumentProposal,
    Evidence,
    Failure,
    JobKey,
    NewDocumentProposal,
    Outcome,
    RefreshResult,
    RelationProposal,
    Usage,
)

RESULT_SCHEMA_VERSION = "refresh-result-v2"


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
        for p in result.document_proposals
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
            "settings": asdict(request.settings),
            "usage": asdict(result.usage),
            "changed_ids": list(changed_ids),
            "target_ids": list(snapshot.target_ids) if snapshot else [],
            "observed_graph_version": snapshot.related.observed_version
            if snapshot
            else None,
        },
    }


def result_from_payload(payload):
    """저장된 v1 결과의 계산 값을 복원한다. 소비자별 메타데이터는 원본 JSON에 보존한다.

    외부 모델 응답 파서가 아니다. 저장 객체의 무결성·프로젝트 권한 검사는 어댑터가
    수행한다. 알려진 필드와 결과 상태·근거 참조가 깨졌으면 ValueError를 발생시킨다.
    """
    try:
        if payload["schema_version"] not in {
            RESULT_SCHEMA_VERSION,
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
                item["evidence"] = tuple(
                    evidence[key] for key in item.pop("evidence_refs")
                )
                result.append(cls(**item))
            return tuple(result)

        execution = payload["execution"]
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
                if payload["schema_version"] == RESULT_SCHEMA_VERSION
                else [],
                NewDocumentProposal,
            ),
        )
        _check_outcome(result)
        return result
    except (KeyError, TypeError) as error:
        raise ValueError("Malformed stored result") from error


def _check_outcome(result):
    has_proposals = bool(
        result.document_proposals
        or result.relation_proposals
        or result.new_document_proposals
    )
    if result.outcome == Outcome.FAILED:
        if has_proposals or result.failure is None:
            raise ValueError("Failed result must have error and no proposals")
    elif (
        result.failure is not None
        or (result.outcome == Outcome.PROPOSED) != has_proposals
    ):
        raise ValueError("Result outcome conflicts with proposals or error")


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
