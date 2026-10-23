from dataclasses import replace

import pytest

from app.refresh.errors import RefreshFailure
from app.refresh.models import DocumentBatch, DocumentState, Relation
from app.refresh.rules import RefreshRules
from tests.fakes.real_rules import changed_documents, real_example


def test_selection_is_deterministic_preserves_revision_and_multi_targets():
    request, source, related, targets, _ = real_example()
    rules = RefreshRules()
    before = repr((request, source, related, targets))
    selection = rules.select(request, source, related)
    assert selection.missing_ids == ("c1", "i1")
    snapshot = rules.assemble(
        request, source, related, selection, DocumentBatch(targets)
    )
    assert snapshot.target_ids == ("c1", "i1")
    assert snapshot.changed_ids == ("m1", "m2")
    assert snapshot == rules.assemble(
        request, source, related, selection, DocumentBatch(tuple(reversed(targets)))
    )
    assert repr((request, source, related, targets)) == before


# 옛 한도(20개·5,000자)를 넘어도 문서 수·본문 길이로 거절하지 않는다.
@pytest.mark.parametrize("count,chars", [(20, 5000), (21, 5000), (20, 5001)])
def test_input_count_and_body_length_are_not_rejected(count, chars):
    request, source = changed_documents(count, chars)
    RefreshRules().validate_input(request, source)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"project_id": "other"}, "PROJECT_MISMATCH"),
        ({"revision_no": 0}, "MANIFEST_MISMATCH"),
        ({"body_text": None}, "BODY_MISSING"),
    ],
)
def test_invalid_document(change, code):
    request, source, *_ = real_example()
    source = replace(
        source, documents=(replace(source.documents[0], **change), source.documents[1])
    )
    with pytest.raises(RefreshFailure) as caught:
        RefreshRules().validate_input(request, source)
    assert caught.value.failure.code == code


@pytest.mark.parametrize("state", [DocumentState.TRASHED, DocumentState.DELETED])
def test_inactive_changes_count_but_do_not_expand(state):
    request, source, related, _, _ = real_example()
    source = replace(
        source,
        documents=tuple(
            replace(d, state=state, body_text=None) for d in source.documents
        ),
    )
    request = replace(
        request,
        changed_documents=tuple(
            replace(item, state=state) for item in request.changed_documents
        ),
    )
    selection = RefreshRules().select(request, source, related)
    assert selection.missing_ids == ()


def test_cycle_duplicate_and_unconnected_ids_do_not_expand_unbounded():
    request, source, related, _, _ = real_example()
    related = replace(
        related,
        document_ids=related.document_ids + ("orphan",),
        relations=related.relations * 2
        + (Relation("project", "c1", "m1", "related_manuscript"),),
    )
    assert RefreshRules().select(request, source, related).missing_ids == ("c1", "i1")


def test_additional_documents_are_not_capped_by_count():
    """관계로 닿은 추가 문서는 옛 기본 예산(40개)을 넘어도 모두 선택한다."""
    request, source, related, _, _ = real_example()
    ids = tuple(f"c{i:02d}" for i in range(41))
    related = replace(
        related,
        document_ids=ids,
        relations=tuple(
            Relation("project", "m1", target, "related_character") for target in ids
        ),
    )
    assert RefreshRules().select(request, source, related).missing_ids == ids


@pytest.mark.parametrize("kind", ["missing", "deleted", "overwrites_input"])
def test_required_references_cannot_be_missing_inactive_or_overwrite_input(kind):
    request, source, related, targets, _ = real_example()
    rules = RefreshRules()
    selection = rules.select(request, source, related)
    fetched = DocumentBatch(targets)
    if kind == "missing":
        fetched = DocumentBatch(targets[:1], ("i1",))
    elif kind == "deleted":
        fetched = DocumentBatch(
            (replace(targets[0], state=DocumentState.DELETED), targets[1])
        )
    else:
        fetched = DocumentBatch(
            targets + (replace(source.documents[0], revision_no=99),)
        )
    with pytest.raises(RefreshFailure):
        rules.assemble(request, source, related, selection, fetched)


def test_fetch_embedded_relation_must_match_owner_and_project():
    request, source, related, targets, _ = real_example()
    rules = RefreshRules()
    relation = Relation("other", "c1", "i1", "related_item")
    targets = (replace(targets[0], relations=(relation,)), targets[1])
    with pytest.raises(RefreshFailure) as caught:
        rules.assemble(
            request,
            source,
            related,
            rules.select(request, source, related),
            DocumentBatch(targets),
        )
    assert caught.value.failure.code == "INVALID_RELATION"


def test_manifest_missing_document_or_different_state_fails():
    request, source, *_ = real_example()
    for manifest in [
        request.changed_documents[:1],
        (
            replace(request.changed_documents[0], state=DocumentState.TRASHED),
            request.changed_documents[1],
        ),
    ]:
        with pytest.raises(RefreshFailure) as caught:
            RefreshRules().validate_input(
                replace(request, changed_documents=manifest), source
            )
        assert caught.value.failure.code == "MANIFEST_MISMATCH"
