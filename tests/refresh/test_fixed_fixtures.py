from tests.fakes.real_rules import changed_documents, real_example


def test_fixtures_cover_multi_source_target_and_new_relation():
    request, source, related, targets, candidate = real_example()
    assert request.seed_ids == ("m1", "m2")
    assert [d.revision_no for d in targets] == [7, 9]
    assert len(candidate.document_proposals[0].evidence) == 2
    assert candidate.relation_proposals[0].description == "유나가 소지한 검"
    assert related.document_ids == ("c1", "i1")
    for proposal in candidate.document_proposals:
        for evidence in proposal.evidence:
            document = next(
                d for d in source.documents if d.document_id == evidence.document_id
            )
            assert document.body_text[evidence.start : evidence.end] == evidence.quote


def test_boundary_fixture_preserves_full_input():
    for count, chars in [(20, 5000), (21, 5000), (20, 5001)]:
        _, source = changed_documents(count, chars)
        assert len(source.documents) == count
        assert all(len(d.body_text) == chars for d in source.documents)
