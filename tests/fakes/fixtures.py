"""Internal examples only; not a finalized wire schema or quality benchmark."""

from app.refresh.models import (
    ArtifactRef, DocumentSnapshot, Evidence, ExecutionSnapshot, InputSnapshot,
    JobKey, ModelCandidate, Outcome, Proposal, RefreshRequest, RefreshResult,
    RelatedDocuments, Relation, SelectionSettings, Usage,
)


def example():
    job = JobKey("project-1", "request-1", "fixture-input-v1")
    request = RefreshRequest(job, ArtifactRef("fake-input", "request-1/input"), ("draft-1",),
                             settings=SelectionSettings(policy_version="fixture-v1"))
    source = DocumentSnapshot("project-1", "draft-1", 3, "유나는 파란 망토를 입었다.")
    setting = DocumentSnapshot("project-1", "setting-1", 7, "유나의 망토는 붉다.",
                               (("name", "유나"),))
    relation = Relation("project-1", "draft-1", "setting-1", "related_character")
    related = RelatedDocuments(("setting-1",), (relation,), observed_version="fixture-graph-v1")
    snapshot = ExecutionSnapshot(request, (source, setting), related)
    evidence = Evidence(source.document_id, source.revision, 0, len(source.body), source.body)
    proposal = Proposal(setting.document_id, setting.revision, "body", "유나의 망토는 파랗다.", (evidence,))
    candidate = ModelCandidate((proposal,), Usage(1, 20, 10))
    result = RefreshResult(job, Outcome.PROPOSED, candidate.proposals, candidate.usage,
                           prompt_version="fixture-v1")
    return request, InputSnapshot(job, (source,)), related, snapshot, candidate, result
