"""역할·수령인·상태·객체 귀속의 독립 자료. 고정 후보는 실행 경로 확인용이다."""

from dataclasses import replace

from app.refresh.facts import fact_description
from app.refresh.models import (
    ArtifactRef,
    DocumentProposal,
    DocumentSnapshot,
    Evidence,
    FactEntity,
    InputSnapshot,
    ModelCandidate,
    RelatedDocuments,
    RelationProposal,
    SourceFact,
    Usage,
)
from app.refresh.policy import RELATION_KEYS
from evaluation.cases import EvaluationCase, cases, evaluation_fingerprint
from evaluation.semantics import (
    ExpectedEntity,
    ExpectedEvent,
    ProposalScope,
    SemanticCriterion,
    SemanticFrame,
)


def _entity(name, document_id=None):
    return FactEntity(name, document_id)


def _expected(name, document_id=None):
    return ExpectedEntity((name,), document_id)


def _fact(text, key, statement, action, **roles):
    return SourceFact(
        key,
        roles.get("actor"),
        action,
        roles.get("recipient"),
        roles.get("object"),
        roles.get("certainty", "CONFIRMED"),
        roles.get("extent", "UNSPECIFIED"),
        roles.get("time", "CURRENT"),
        statement,
        # v5 restores the selected source chunk, not a model-selected excerpt.
        # This preserves the source without proving that the statement follows.
        (Evidence("m1", 3, 0, len(text), text),),
    )


def _case(model, trial, name, text, documents, facts, links, criteria):
    base = cases(model, trial)[0]
    manuscript = replace(base.source.documents[0], body_text=text)
    targets = tuple(
        DocumentSnapshot(
            "evaluation",
            document_id,
            7,
            "아직 확인한 설정이 없다.",
            properties=(("name", title),),
            folder_code=code,
            title=title,
        )
        for document_id, title, code, _, _ in documents
    )
    fact_map = {fact.fact_id: fact for fact in facts}

    def evidence(refs):
        return tuple(dict.fromkeys(e for key in refs for e in fact_map[key].evidence))

    proposals = tuple(
        DocumentProposal(document_id, 7, "body_text", body, evidence(refs), refs)
        for document_id, _, _, body, refs in documents
    )
    codes = {document.document_id: document.folder_code for document in targets}
    relations = tuple(
        RelationProposal(
            first,
            second,
            7,
            7,
            RELATION_KEYS[codes[second]],
            RELATION_KEYS[codes[first]],
            fact_description(fact_map[key] for key in refs),
            evidence(refs),
            fact_ids=refs,
        )
        for first, second, refs in links
    )
    job = replace(
        base.request.job,
        request_id=f"{trial}-{name}",
        input_fingerprint=evaluation_fingerprint((manuscript, targets)),
    )
    return EvaluationCase(
        name,
        replace(
            base.request,
            job=job,
            input_ref=ArtifactRef("evaluation", f"{name}/input.json"),
            target_ids=tuple(document.document_id for document in targets),
        ),
        InputSnapshot(job, (manuscript,)),
        RelatedDocuments(tuple(document.document_id for document in targets)),
        targets,
        ModelCandidate(proposals, Usage(1), relations, facts=facts),
        semantic_criteria=criteria,
    )


def _roles(model, trial, names, reordered):
    approver, carrier, receiver = names
    approval = f"{approver}이 {carrier}의 기억 물통 운반을 승인한다."
    transport = f"{carrier}이 {receiver}에게 기억 물통을 운반한다."
    receipt = f"{receiver}는 기억 일부를 돌려받는다."
    text = " ".join(
        (receipt, approval, transport) if reordered else (approval, transport, receipt)
    )
    facts = (
        _fact(
            text,
            "approval",
            approval,
            "승인한다",
            actor=_entity(approver, "approver"),
            recipient=_entity(carrier, "carrier"),
            object=_entity("기억 물통"),
        ),
        _fact(
            text,
            "transport",
            transport,
            "운반한다",
            actor=_entity(carrier, "carrier"),
            recipient=_entity(receiver, "receiver"),
            object=_entity("기억 물통"),
        ),
        _fact(
            text,
            "receipt",
            receipt,
            "돌려받는다",
            recipient=_entity(receiver, "receiver"),
            object=_entity("기억"),
            extent="PARTIAL",
        ),
    )
    criteria = (
        SemanticCriterion(
            "운반 승인 담당",
            ("승인한다", "허가한다"),
            actor=_expected(approver, "approver"),
            recipient=_expected(carrier, "carrier"),
            object=_expected("기억 물통"),
            certainty="CONFIRMED",
            scope=ProposalScope("relation", "approver", "carrier"),
            alternative_frames=(
                SemanticFrame(
                    actor=_expected(approver, "approver"),
                    recipient=_expected(carrier, "carrier"),
                    object=ExpectedEvent(
                        _expected(carrier, "carrier"),
                        _expected("기억 물통"),
                        ("운반",),
                        allow_unqualified=True,
                    ),
                ),
                SemanticFrame(
                    actor=_expected(approver, "approver"),
                    object=ExpectedEvent(
                        _expected(carrier, "carrier"),
                        _expected("기억 물통"),
                        ("운반",),
                    ),
                    absent_roles=("recipient",),
                ),
            ),
        ),
        SemanticCriterion(
            "운반자와 수령인",
            ("운반한다", "전달한다"),
            actor=_expected(carrier, "carrier"),
            recipient=_expected(receiver, "receiver"),
            object=_expected("기억 물통"),
            certainty="CONFIRMED",
            scope=ProposalScope("relation", "carrier", "receiver"),
        ),
        SemanticCriterion(
            "부분 반환 수령인",
            ("돌려받는다", "반환받는다"),
            recipient=_expected(receiver, "receiver"),
            extent="PARTIAL",
            certainty="CONFIRMED",
            scope=ProposalScope("document", "receiver"),
        ),
    )
    return _case(
        model,
        trial,
        "semantic-roles-reordered" if reordered else "semantic-roles",
        text,
        (
            ("approver", approver, "CHARACTER", approval, ("approval",)),
            (
                "carrier",
                carrier,
                "CHARACTER",
                f"{carrier}은 승인받아 물통을 나르고 {receiver}에게 전달한다.",
                ("approval", "transport"),
            ),
            (
                "receiver",
                receiver,
                "CHARACTER",
                f"{receiver}는 기억을 일부 되찾았다.",
                ("transport", "receipt"),
            ),
        ),
        facts,
        (
            ("approver", "carrier", ("approval",)),
            ("carrier", "receiver", ("transport",)),
        ),
        criteria,
    )


def _partial(model, trial):
    returned = "은서는 가족 기억 일부를 돌려받았다."
    absent = "건우는 가족 기억을 돌려받지 않았다."
    text = f"{returned} {absent}"
    facts = (
        _fact(
            text,
            "partial",
            returned,
            "돌려받았다",
            recipient=_entity("은서", "mother"),
            object=_entity("가족 기억", "memory"),
            extent="PARTIAL",
        ),
        _fact(
            text,
            "not-returned",
            absent,
            "돌려받지 않았다",
            recipient=_entity("건우", "son"),
            object=_entity("가족 기억", "memory"),
        ),
    )
    returned_criterion = SemanticCriterion(
        "실제 반환 수령인",
        ("돌려받았다", "반환받았다", "되찾았다"),
        recipient=_expected("은서", "mother"),
        object=_expected("가족 기억", "memory"),
        certainty="CONFIRMED",
        extent="PARTIAL",
        scope=ProposalScope("relation", "mother", "memory"),
    )
    criteria = (
        returned_criterion,
        replace(
            returned_criterion,
            name="본문의 반환 수령인",
            scope=ProposalScope("document", "mother"),
        ),
        SemanticCriterion(
            "아들은 미반환",
            ("돌려받지 않았다", "반환받지 않았다"),
            recipient=_expected("건우", "son"),
            object=_expected("가족 기억", "memory"),
            certainty="CONFIRMED",
            scope=ProposalScope("relation", "son", "memory"),
        ),
        replace(
            returned_criterion,
            name="전체 회복 단정 금지",
            extent="COMPLETE",
            forbidden=True,
        ),
    )
    return _case(
        model,
        trial,
        "semantic-partial-recipient",
        text,
        (
            (
                "mother",
                "은서",
                "CHARACTER",
                "은서는 가족 기억을 조금 되찾았다.",
                ("partial",),
            ),
            (
                "son",
                "건우",
                "CHARACTER",
                "건우에게는 가족 기억이 반환되지 않았다.",
                ("not-returned",),
            ),
            (
                "memory",
                "가족 기억",
                "ITEM",
                "가족 기억의 일부는 은서에게 돌아왔고 건우에게는 돌아오지 않았다.",
                ("partial", "not-returned"),
            ),
        ),
        facts,
        (("mother", "memory", ("partial",)), ("son", "memory", ("not-returned",))),
        criteria,
    )


def _state(model, trial):
    recorded = "지난달 접수 장부에는 예진의 기억이 반환 완료라고 기록되어 있었다."
    actual = "현재 실제 기억 물통은 창고에 남아 예진에게 전달되지 않았다."
    planned = (
        "상우는 내일 예진의 지금 동의를 확인한 뒤 기억 물통의 반환을 승인할 계획이다."
    )
    text = f"{recorded} {actual} {planned}"
    facts = (
        _fact(
            text,
            "past-record",
            recorded,
            "반환 완료라고 기록되어 있었다",
            actor=_entity("접수 장부"),
            recipient=_entity("예진", "applicant"),
            object=_entity("기억"),
            time="PAST",
        ),
        _fact(
            text,
            "not-delivered",
            actual,
            "전달되지 않았다",
            recipient=_entity("예진", "applicant"),
            object=_entity("기억 물통", "tube"),
        ),
        _fact(
            text,
            "future-approval",
            planned,
            "승인할 계획이다",
            actor=_entity("상우", "approver"),
            recipient=_entity("예진", "applicant"),
            object=_entity("기억 물통", "tube"),
            certainty="PLANNED",
            time="FUTURE",
        ),
    )
    scope = ProposalScope("document", "applicant")
    criteria = (
        SemanticCriterion(
            "과거 장부의 완료 표기",
            ("반환 완료라고 기록되어 있었다", "반환 완료로 적혀 있었다"),
            recipient=_expected("예진", "applicant"),
            time="PAST",
            scope=scope,
        ),
        SemanticCriterion(
            "실제로는 미전달",
            ("전달되지 않았다", "아직 전달하지 않았다"),
            recipient=_expected("예진", "applicant"),
            object=_expected("기억 물통", "tube"),
            certainty="CONFIRMED",
            time="CURRENT",
            scope=scope,
        ),
        SemanticCriterion(
            "현재 동의 뒤 승인 예정",
            ("승인할 계획이다", "승인할 예정이다"),
            actor=_expected("상우", "approver"),
            recipient=_expected("예진", "applicant"),
            certainty="PLANNED",
            time="FUTURE",
            scope=scope,
            alternative_frames=(
                SemanticFrame(
                    actor=_expected("상우", "approver"),
                    object=ExpectedEvent(
                        _expected("예진", "applicant"),
                        _expected("기억 물통", "tube"),
                        ("반환",),
                        templates=(
                            "{participant}에게 {object} {action}",
                            "{participant}에게 {object}의 {action}",
                        ),
                    ),
                    absent_roles=("recipient",),
                ),
            ),
        ),
    )
    return _case(
        model,
        trial,
        "semantic-record-and-consent",
        text,
        (
            (
                "applicant",
                "예진",
                "CHARACTER",
                "예진의 과거 완료 기록과 달리 물통은 미전달 상태다. 현재 동의를 확인한 뒤 승인이 예정되어 있다.",
                ("past-record", "not-delivered", "future-approval"),
            ),
            (
                "approver",
                "상우",
                "CHARACTER",
                "상우는 현재 동의 확인 후 반환을 승인할 예정이다.",
                ("future-approval",),
            ),
            (
                "tube",
                "기억 물통",
                "ITEM",
                "물통은 창고에 남아 있고 아직 예진에게 전달되지 않았다.",
                ("not-delivered",),
            ),
        ),
        facts,
        (
            ("approver", "applicant", ("future-approval",)),
            ("applicant", "tube", ("not-delivered",)),
        ),
        criteria,
    )


def _objects(model, trial):
    label = "유리통에는 이름과 반환 번호 라벨이 붙어 있다."
    seal = "나무 상자는 구리 봉인으로 잠겨 있다."
    contains = "나무 상자는 유리통을 담는다."
    text = f"{label} {seal} {contains}"
    facts = (
        _fact(
            text,
            "label",
            label,
            "붙어 있다",
            actor=_entity("유리통", "glass"),
            object=_entity("이름과 반환 번호 라벨"),
        ),
        _fact(
            text,
            "seal",
            seal,
            "잠겨 있다",
            actor=_entity("나무 상자", "box"),
            object=_entity("구리 봉인"),
        ),
        _fact(
            text,
            "contains",
            contains,
            "담는다",
            actor=_entity("나무 상자", "box"),
            object=_entity("유리통", "glass"),
        ),
    )
    label_criterion = SemanticCriterion(
        "라벨은 유리통 속성",
        ("붙어 있다", "부착되어 있다"),
        actor=_expected("유리통", "glass"),
        object=_expected("이름과 반환 번호 라벨"),
        certainty="CONFIRMED",
        scope=ProposalScope("document", "glass"),
    )
    seal_criterion = SemanticCriterion(
        "구리 봉인은 상자 속성",
        ("잠겨 있다", "봉인되어 있다"),
        actor=_expected("나무 상자", "box"),
        object=_expected("구리 봉인"),
        certainty="CONFIRMED",
        scope=ProposalScope("document", "box"),
    )
    criteria = (
        label_criterion,
        seal_criterion,
        SemanticCriterion(
            "상자가 유리통을 담음",
            ("담는다", "담고 있다"),
            actor=_expected("나무 상자", "box"),
            object=_expected("유리통", "glass"),
            certainty="CONFIRMED",
            scope=ProposalScope("relation", "box", "glass"),
        ),
        replace(
            label_criterion,
            name="라벨의 상자 귀속 금지",
            actor=_expected("나무 상자", "box"),
            scope=ProposalScope("document", "box"),
            forbidden=True,
        ),
        replace(
            seal_criterion,
            name="봉인의 유리통 귀속 금지",
            actor=_expected("유리통", "glass"),
            scope=ProposalScope("document", "glass"),
            forbidden=True,
        ),
    )
    return _case(
        model,
        trial,
        "semantic-object-properties",
        text,
        (
            (
                "glass",
                "유리통",
                "ITEM",
                "유리통에는 이름과 반환 번호를 표시한 라벨이 있다.",
                ("label", "contains"),
            ),
            (
                "box",
                "나무 상자",
                "ITEM",
                "유리통을 담는 나무 상자는 구리로 봉인되어 있다.",
                ("seal", "contains"),
            ),
        ),
        facts,
        (("box", "glass", ("contains",)),),
        criteria,
    )


def _anonymous(model, trial):
    organized = "오늘 전시 안내소에서 연서는 보관함을 정리했다."
    handed = "전달 담당자는 이름표가 없는 방문객에게 녹색 봉투를 건넸다."
    absent = "연서는 전달 절차를 승인하거나 봉투를 건네지 않았다."
    text = f"{organized} {handed} {absent}"
    facts = (
        _fact(
            text,
            "organized",
            organized,
            "정리했다",
            actor=_entity("연서", "character-yeonseo"),
            object=_entity("보관함"),
        ),
        _fact(
            text,
            "anonymous-handoff",
            handed,
            "건넸다",
            actor=_entity("전달 담당자"),
            recipient=_entity("이름표가 없는 방문객"),
            object=_entity("녹색 봉투", "item-envelope"),
        ),
        _fact(
            text,
            "not-handed",
            absent,
            "승인하거나 봉투를 건네지 않았다",
            actor=_entity("연서", "character-yeonseo"),
            object=_entity("봉투", "item-envelope"),
        ),
    )
    criteria = (
        SemanticCriterion(
            "익명 전달 담당자와 방문객",
            ("건넸다", "건네었다", "전달했다"),
            actor=_expected("전달 담당자"),
            recipient=_expected("이름표가 없는 방문객"),
            object=_expected("녹색 봉투", "item-envelope"),
            certainty="CONFIRMED",
            time="CURRENT",
            scope=ProposalScope("document", "item-envelope"),
        ),
        SemanticCriterion(
            "연서는 보관함만 정리",
            ("정리했다",),
            actor=_expected("연서", "character-yeonseo"),
            object=_expected("보관함"),
            certainty="CONFIRMED",
            time="CURRENT",
            scope=ProposalScope("document", "character-yeonseo"),
        ),
        SemanticCriterion(
            "연서는 승인과 전달을 하지 않음",
            ("승인하거나 봉투를 건네지 않았다",),
            actor=_expected("연서", "character-yeonseo"),
            object=ExpectedEntity(("녹색 봉투", "봉투"), "item-envelope"),
            certainty="CONFIRMED",
            time="CURRENT",
            scope=ProposalScope("document", "character-yeonseo"),
        ),
        SemanticCriterion(
            "주변 인물을 전달 담당자로 귀속 금지",
            ("건넸다", "건네었다", "전달했다"),
            actor=_expected("연서", "character-yeonseo"),
            forbidden=True,
        ),
    )
    return _case(
        model,
        trial,
        "semantic-anonymous-attribution",
        text,
        (
            (
                "character-yeonseo",
                "연서",
                "CHARACTER",
                "연서는 전시 안내소에서 보관함을 정리했다. 봉투 전달을 승인하거나 수행하지 않았다.",
                ("organized", "not-handed"),
            ),
            (
                "item-envelope",
                "녹색 봉투",
                "ITEM",
                "녹색 봉투는 이름이 밝혀지지 않은 전달 담당자가 이름표 없는 방문객에게 건넸다.",
                ("anonymous-handoff",),
            ),
        ),
        facts,
        (),
        criteria,
    )


def semantic_cases(model: str, trial: str) -> tuple[EvaluationCase, ...]:
    """고유 이름·문장 순서가 다른 역할 사례와 세 가지 오귀속 자료를 제공한다."""
    return (
        _roles(model, trial, ("라민", "수진", "태오"), False),
        _roles(model, trial, ("다온", "하린", "시우"), True),
        _partial(model, trial),
        _state(model, trial),
        _objects(model, trial),
        _anonymous(model, trial),
    )
