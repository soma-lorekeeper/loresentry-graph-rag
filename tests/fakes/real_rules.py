"""실제 rules 검증용 고정 자료. 모델 품질 평가 자료는 아니다."""

from dataclasses import replace

from app.refresh.models import (
    ArtifactRef,
    DocumentProposal,
    DocumentSnapshot,
    Evidence,
    InputSnapshot,
    JobKey,
    ModelCandidate,
    RefreshRequest,
    RelatedDocuments,
    Relation,
    RelationProposal,
    Usage,
)


def real_example():
    job = JobKey("project", "request", "fixed-input-v1")
    request = RefreshRequest(job, ArtifactRef("input", "input.json"), ("m1", "m2"))
    documents = (
        DocumentSnapshot(
            "project", "m1", 3, "유나는 파란 망토를 입었다.\n😀 다시 만났다."
        ),
        DocumentSnapshot(
            "project", "m2", 5, "유나는 은색 검을 들었다. 같은 말. 같은 말."
        ),
    )
    targets = (
        DocumentSnapshot(
            "project", "c1", 7, "유나는 붉은 망토를 입는다.", folder_code="CHARACTER"
        ),
        DocumentSnapshot("project", "i1", 9, "검은 청동이다.", folder_code="ITEM"),
    )
    relations = (
        Relation("project", "m1", "c1", "related_character"),
        Relation("project", "m2", "c1", "related_character"),
        Relation("project", "m2", "i1", "related_item"),
    )
    related = RelatedDocuments(("c1", "i1"), relations)
    evidence = tuple(
        Evidence(d.document_id, d.revision_no, 0, len(d.body_text), d.body_text)
        for d in documents
    )
    proposals = (
        DocumentProposal(
            "c1", 7, "body_text", "유나는 파란 망토를 입고 은색 검을 든다.", evidence
        ),
        DocumentProposal("i1", 9, "body_text", "검은 은색이다.", evidence[1:]),
    )
    relation = RelationProposal(
        "c1",
        "i1",
        7,
        9,
        "related_item",
        "related_character",
        "유나가 소지한 검",
        evidence,
    )
    candidate = ModelCandidate(proposals, Usage(1, 100, 50), (relation,))
    return request, InputSnapshot(job, documents), related, targets, candidate


def changed_documents(count=20, chars=5000):
    request, source, *_ = real_example()
    documents = tuple(
        replace(source.documents[0], document_id=f"m{i}", body_text="가" * chars)
        for i in range(count)
    )
    return replace(request, seed_ids=tuple(d.document_id for d in documents)), replace(
        source, documents=documents
    )
