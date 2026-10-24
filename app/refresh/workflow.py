"""문단 ID 계획 검증·작성 문맥·본문 조립. 원고·Content 문서를 직접 변경하지 않는다."""

import json
import re
from dataclasses import asdict, dataclass, replace

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import unique_evidence
from app.refresh.facts import fact_description
from app.refresh.models import (
    ChangePlan,
    DocumentProposal,
    Failure,
    ParagraphCopyRemoval,
    ParagraphEdit,
    SourceScopeExtension,
    Usage,
    ValidationDiagnostic,
    WrittenCandidate,
)
from app.refresh.policy import RELATION_KEYS

PLAN_SCHEMA = "refresh-plan-v1"
WRITE_SCHEMAS = frozenset({"refresh-candidate-v7", "refresh-candidate-v8"})
WORKFLOW_PROMPT_VERSION = "refresh-prompt-v13"


@dataclass(frozen=True)
class Paragraph:
    document_id: str
    base_revision_no: int
    paragraph_id: str
    start: int
    end: int
    text: str


def current_bodies(snapshot):
    """대상 설정의 현재 본문. 묶음 실행에서 앞 묶음이 바꾼 seed는 원문 대신 그 본문이다."""
    working = dict(getattr(snapshot, "working_bodies", ()))
    return {
        d.document_id: working.get(d.document_id, d.body_text or "")
        for d in snapshot.documents
        if d.document_id in snapshot.target_ids
    }


def paragraphs(snapshot):
    """빈 줄 구분과 원문 위치를 보존하여 대상 설정에만 문단 ID를 붙인다."""
    result = []
    bodies = current_bodies(snapshot)
    for document in snapshot.documents:
        if document.document_id not in snapshot.target_ids:
            continue
        body = bodies[document.document_id]
        matches = list(
            re.finditer(r"\S(?:.*?\S)?(?=\n[ \t]*\n|[ \t\r\n]*\Z)", body, re.S)
        )
        if not matches:
            result.append(
                Paragraph(document.document_id, document.revision_no, "P0000", 0, 0, "")
            )
        for index, match in enumerate(matches, 1):
            result.append(
                Paragraph(
                    document.document_id,
                    document.revision_no,
                    f"P{index:04d}",
                    match.start(),
                    match.end(),
                    match.group(),
                )
            )
    return tuple(result)


def plan_payload(plan):
    value = asdict(plan)
    value.pop("usage")
    return value


def load_plan(value):
    from app.refresh.models import PlannedEdit, PlannedNewDocument, PlannedRelation

    return ChangePlan(
        tuple(value["reviewed_document_ids"]),
        tuple(
            PlannedEdit(**{**item, "source_markers": tuple(item["source_markers"])})
            for item in value["edits"]
        ),
        tuple(
            PlannedNewDocument(
                **{**item, "source_markers": tuple(item["source_markers"])}
            )
            for item in value["new_documents"]
        ),
        tuple(
            PlannedRelation(**{**item, "source_markers": tuple(item["source_markers"])})
            for item in value["relations"]
        ),
        Usage(**value["usage"]) if "usage" in value else Usage(),
    )


def plan_fail(message, path="change_plan", rule="plan.complete", value=None):
    raise RefreshFailure(
        Failure("INVALID_CHANGE_PLAN", message),
        diagnostics=(
            ValidationDiagnostic(
                path, rule, offending_value=None if value is None else str(value)[:200]
            ),
        ),
    )


def _bounded(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def validate_plan(snapshot, plan, model_input):
    """범위·revision·작업 중복·출처 마커를 검사한다. 의미상 완전성은 보증하지 않는다.

    방향만 다른 같은 관계는 하나로 합친 계획을 반환한다.
    """
    if not isinstance(plan, ChangePlan):
        plan_fail("Typed change plan required")
    targets = snapshot.target_ids
    if len(plan.reviewed_document_ids) != len(set(plan.reviewed_document_ids)) or set(
        plan.reviewed_document_ids
    ) != set(targets):
        plan_fail(
            "Every selected settings document must be reviewed",
            "reviewed_document_ids",
            "plan.targets_reviewed",
        )
    context = json.loads(model_input.prompt)
    markers = {item["marker"]: item for item in context["evidence"]}
    inventory = {(p.document_id, p.paragraph_id): p for p in paragraphs(snapshot)}
    existing = {d.document_id: d for d in snapshot.documents}
    edit_ids, positions = set(), set()

    def sources(item, path):
        invalid = [
            marker
            for marker in item.source_markers
            if marker not in markers
            or markers[marker]["document_id"] not in snapshot.changed_ids
        ]
        if (
            not item.source_markers
            or len(set(item.source_markers)) != len(item.source_markers)
            or invalid
        ):
            plan_fail(
                "Plan requires unique changed-source markers",
                path + ".source_markers",
                "plan.source_markers",
                ", ".join(invalid) or None,
            )

    if (
        len(plan.edits) > len(inventory) * 2
        or len(plan.new_documents) > 20
        or len(plan.relations) > 100
    ):
        plan_fail("Change plan budget exceeded")
    if plan.edits and not context["allow_document_updates"]:
        plan_fail(
            "Document updates are outside this call's scope",
            "edits",
            "plan.edits_allowed",
        )
    if plan.new_documents and not context["allow_new_documents"]:
        plan_fail(
            "New settings are outside this call's scope",
            "new_documents",
            "plan.new_allowed",
        )
    for index, edit in enumerate(plan.edits):
        paragraph = inventory.get((edit.document_id, edit.paragraph_id))
        if (
            paragraph is None
            or type(edit.base_revision_no) is not int
            or edit.base_revision_no != paragraph.base_revision_no
        ):
            plan_fail(
                "Edit must identify an existing target paragraph and revision",
                f"edits[{index}]",
                "plan.paragraph_known",
                f"{edit.document_id} {edit.paragraph_id} r{edit.base_revision_no}",
            )
        if (
            edit.operation not in {"REPLACE", "DELETE", "APPEND"}
            or (edit.paragraph_id == "P0000" and edit.operation != "APPEND")
            or not _bounded(edit.edit_id, 200)
            or edit.edit_id in edit_ids
            or not _bounded(edit.instruction, 2000)
        ):
            plan_fail(
                "Unique bounded edit and instruction required",
                f"edits[{index}]",
                "plan.edit_valid",
                edit.edit_id,
            )
        position = (
            edit.document_id,
            edit.paragraph_id,
            "APPEND" if edit.operation == "APPEND" else "CHANGE",
        )
        if position in positions:
            plan_fail(
                "Only one replacement/deletion and one append per paragraph",
                f"edits[{index}]",
                "plan.position_unique",
                f"{edit.document_id} {edit.paragraph_id} {edit.operation}",
            )
        positions.add(position)
        edit_ids.add(edit.edit_id)
        sources(edit, f"edits[{index}]")
    from app.refresh.validation import existing_setting_names, setting_name

    existing_names = existing_setting_names(snapshot)
    new_ids, names = set(), set()
    for index, item in enumerate(plan.new_documents):
        if (
            item.folder_code not in RELATION_KEYS
            or item.folder_code == "MANUSCRIPT"
            or not _bounded(item.name, 200)
            or not _bounded(item.instruction, 2000)
            or item.candidate_id != f"new:{item.folder_code}:{item.name}"
            or item.candidate_id in existing
            or item.candidate_id in new_ids
            or (item.folder_code, setting_name(item.name)) in names
        ):
            plan_fail(
                "Unique settings identity required",
                f"new_documents[{index}]",
                "plan.new_identity",
                item.candidate_id,
            )
        if (item.folder_code, setting_name(item.name)) in existing_names:
            plan_fail(
                "New setting duplicates an existing setting name",
                f"new_documents[{index}].name",
                "plan.new_existing",
                item.name,
            )
        new_ids.add(item.candidate_id)
        names.add((item.folder_code, setting_name(item.name)))
        sources(item, f"new_documents[{index}]")
    scope = context["relation_scope"]
    allowed_pairs = {tuple(sorted(pair)) for pair in scope["allowed_existing_pairs"]}
    relations = {}
    for index, item in enumerate(plan.relations):
        pair = tuple(sorted((item.document_id, item.target_document_id)))
        new_endpoint = bool(set(pair) & new_ids)
        if (
            len(set(pair)) != 2
            or any(key not in existing and key not in new_ids for key in pair)
            or (new_endpoint and not scope["allow_new_endpoint_relations"])
            or (not new_endpoint and pair not in allowed_pairs)
        ):
            plan_fail(
                "Unique allowed new relation required",
                f"relations[{index}]",
                "plan.relation_allowed",
                f"{item.document_id} <-> {item.target_document_id}",
            )
        sources(item, f"relations[{index}]")
        previous = relations.get(pair)
        relations[pair] = (
            item
            if previous is None
            else replace(
                previous,
                source_markers=tuple(
                    dict.fromkeys(previous.source_markers + item.source_markers)
                ),
            )
        )
    return replace(plan, relations=tuple(relations.values()))


def _input(snapshot, base, *, plan=None):
    from app.refresh.workflow_prompts import plan_instructions, write_instructions

    context = json.loads(base.prompt)
    context["paragraphs"] = [asdict(p) for p in paragraphs(snapshot)]
    context["workflow"] = "refresh-two-stage-v1"
    context["settings_bodies"] = current_bodies(snapshot)
    if plan is not None:
        context["change_plan"] = plan_payload(plan)
    instructions = (
        plan_instructions()
        if plan is None
        else write_instructions(base.schema_version == "refresh-candidate-v8")
    )
    instructions += "\nFOLDER_RELATION_KEYS=" + json.dumps(
        RELATION_KEYS, sort_keys=True
    )
    prompt = json.dumps(context, ensure_ascii=False, sort_keys=True)
    return replace(
        base,
        prompt=prompt,
        schema_version=PLAN_SCHEMA if plan is None else base.schema_version,
        prompt_version=WORKFLOW_PROMPT_VERSION,
        instructions=instructions,
    )


def build_plan_input(snapshot, base):
    return _input(snapshot, base)


def build_write_input(snapshot, base, plan):
    return _input(snapshot, base, plan=plan)


def assemble_paragraphs(plan, edits, model_input, facts):
    """모든 작업을 검사하고 편집하지 않은 원문을 보존하면서 본문을 조립한다."""
    context = json.loads(model_input.prompt)
    inventory = {
        (p["document_id"], p["paragraph_id"]): p for p in context["paragraphs"]
    }
    documents = {item["document_id"]: item for item in context["documents"]}
    indexed = {edit.edit_id: edit for edit in edits}
    expected = {edit.edit_id for edit in plan.edits}
    if len(indexed) != len(edits) or set(indexed) != expected:
        plan_fail(
            "Every planned edit must be returned exactly once",
            "paragraph_edits",
            "plan.edits_complete",
            f"missing: {', '.join(sorted(expected - set(indexed))) or '-'}; "
            f"unplanned: {', '.join(sorted(set(indexed) - expected)) or '-'}",
        )
    grouped = {}
    for operation in plan.edits:
        edit = indexed[operation.edit_id]
        if not edit.fact_ids or any(key not in facts for key in edit.fact_ids):
            plan_fail(
                "Paragraph edit requires known facts",
                "paragraph_edits",
                "plan.facts_known",
            )
        if (operation.operation == "DELETE" and edit.text != "") or (
            operation.operation != "DELETE" and not edit.text.strip()
        ):
            plan_fail(
                "Only DELETE edits may have empty text",
                "paragraph_edits",
                "plan.text_valid",
            )
        if (
            operation.operation == "REPLACE"
            and edit.text
            == inventory[(operation.document_id, operation.paragraph_id)]["text"]
        ):
            plan_fail(
                "Replacement must change its planned paragraph",
                "paragraph_edits",
                "plan.edit_effective",
            )
        if len(edit.text) > 5000:
            plan_fail(
                "Paragraph exceeds body budget", "paragraph_edits", "plan.text_bounded"
            )
        if any(
            fact.evidence[0]
            not in {
                facts_marker(context, item["marker"])
                for item in context["evidence"]
                if item["document_id"] in context["changed_ids"]
            }
            for key in edit.fact_ids
            for fact in (facts[key],)
        ):
            plan_fail(
                "Edit facts must come from the current changed manuscripts",
                "paragraph_edits",
                "plan.source_coverage",
            )
        paragraph = inventory[(operation.document_id, operation.paragraph_id)]
        grouped.setdefault(operation.document_id, []).append(
            (operation, edit, paragraph)
        )
    result = []
    for document_id, operations in sorted(grouped.items()):
        body = context["settings_bodies"][document_id]
        changes = {}
        appends = {}
        for operation, edit, paragraph in operations:
            if operation.operation == "APPEND":
                appends[paragraph["end"]] = (
                    "\n\n" if paragraph["end"] else ""
                ) + edit.text
            else:
                changes[paragraph["start"]] = (paragraph["end"], edit.text)
        parts, cursor = [], 0
        for point in sorted(set(changes) | set(appends)):
            parts.append(body[cursor:point])
            if point in changes:
                end, text = changes[point]
                parts.append(text)
                cursor = end
            else:
                cursor = point
            if point in appends:
                parts.append(appends[point])
        parts.append(body[cursor:])
        value = "".join(parts)
        if value == body:
            plan_fail(
                "Planned edit left the body unchanged",
                "paragraph_edits",
                "plan.edit_effective",
            )
        refs = tuple(
            dict.fromkeys(key for _, edit, _ in operations for key in edit.fact_ids)
        )
        evidence = unique_evidence(
            source for key in refs for source in facts[key].evidence
        )
        result.append(
            DocumentProposal(
                document_id,
                documents[document_id]["revision_no"],
                "body_text",
                value,
                evidence,
                refs,
            )
        )
    return tuple(result)


def facts_marker(context, marker):
    from app.refresh.models import Evidence

    item = next(item for item in context["evidence"] if item["marker"] == marker)
    return Evidence(
        **{
            key: item[key]
            for key in ("document_id", "revision_no", "start", "end", "quote")
        }
    )


def validate_written_scope(plan, candidate, model_input):
    """계획한 신규 설정의 누락과 정체 변경을 검사하고 관계를 계획 범위로 맞춘다.

    계획에 없는 관계는 버리고, 빠진 관계는 제안하지 않으며, 같은 쌍은 하나로 합친다.
    관계 키와 기준 revision은 끝점의 유형·revision에서 서버가 채운다.
    """
    new = {
        (p.candidate_id, p.folder_code, p.name): p
        for p in candidate.new_document_proposals
    }
    expected = {(p.candidate_id, p.folder_code, p.name): p for p in plan.new_documents}
    if len(new) != len(candidate.new_document_proposals) or set(new) != set(expected):
        missing = [key[0] for key in expected if key not in new]
        extra = [key[0] for key in new if key not in expected]
        plan_fail(
            "Every planned new setting must be returned once",
            "new_document_proposals",
            "plan.new_complete",
            f"missing: {', '.join(missing) or '-'}; unplanned: {', '.join(extra) or '-'}",
        )
    context = json.loads(model_input.prompt)
    endpoints = {
        item["document_id"]: (item["folder_code"], item["revision_no"])
        for item in context["documents"]
    }
    endpoints.update(
        {item.candidate_id: (item.folder_code, None) for item in plan.new_documents}
    )
    planned = {
        tuple(sorted((p.document_id, p.target_document_id))) for p in plan.relations
    }
    facts = {fact.fact_id: fact for fact in candidate.facts}
    relation = {}
    for proposal in candidate.relation_proposals:
        pair = tuple(sorted((proposal.document_id, proposal.target_document_id)))
        if pair not in planned:
            continue
        previous = relation.get(pair)
        if previous is not None:
            refs = tuple(dict.fromkeys(previous.fact_ids + proposal.fact_ids))
            selected = [facts[key] for key in refs]
            proposal = replace(
                previous,
                fact_ids=refs,
                description=fact_description(selected),
                evidence=unique_evidence(e for fact in selected for e in fact.evidence),
            )
        source, target = (
            endpoints.get(proposal.document_id),
            endpoints.get(proposal.target_document_id),
        )
        if source is not None and target is not None:
            proposal = replace(
                proposal,
                relation_key=RELATION_KEYS.get(target[0]),
                reverse_relation_key=RELATION_KEYS.get(source[0]),
                base_revision_no=source[1],
                target_base_revision_no=target[1],
            )
        relation[pair] = proposal
    candidate = replace(candidate, relation_proposals=tuple(relation.values()))
    for _key, proposal in list(new.items()) + list(relation.items()):
        allowed = {
            facts_marker(context, item["marker"])
            for item in context["evidence"]
            if item["document_id"] in context["changed_ids"]
        }
        if any(source not in allowed for source in proposal.evidence):
            plan_fail(
                "Output sources must come from the current changed manuscripts",
                rule="plan.source_coverage",
            )
    return candidate


def materialize_candidate(dto, usage, model_input):
    """새 전송 DTO를 기존 사실 파서·검증이 사용하는 내부 후보로 변환한다."""
    from app.adapters.llm_schema import parse_candidate

    payload = dto.model_dump()
    edits = payload.pop("paragraph_edits")
    payload["document_proposals"] = []
    legacy_schema = (
        "refresh-candidate-v6"
        if model_input.schema_version == "refresh-candidate-v8"
        else "refresh-candidate-v5"
    )
    candidate = parse_candidate(
        json.dumps(payload, ensure_ascii=False),
        usage,
        schema_version=legacy_schema,
        model_input=replace(model_input, schema_version=legacy_schema),
    )
    declared = payload["facts"]
    fact_refs = {}
    # 파서가 fact_id를 canonical ID로 바꾸므로 같은 DTO 사실을 단독 변환해 대응한다.
    for item in declared:
        one = {
            "facts": [item],
            "document_proposals": [],
            "new_document_proposals": [],
            "relation_proposals": [],
        }
        parsed = parse_candidate(
            json.dumps(one, ensure_ascii=False),
            usage,
            schema_version=legacy_schema,
            model_input=replace(model_input, schema_version=legacy_schema),
        )
        fact_refs[item["fact_id"]] = parsed.facts[0].fact_id
    typed = []
    for edit in edits:
        if len(edit["fact_ids"]) != len(set(edit["fact_ids"])) or any(
            key not in fact_refs for key in edit["fact_ids"]
        ):
            plan_fail("Edit requires unique declared facts", rule="plan.facts_known")
        typed.append(
            ParagraphEdit(
                edit["edit_id"],
                edit["text"],
                tuple(fact_refs[key] for key in edit["fact_ids"]),
            )
        )
    plan = load_plan(json.loads(model_input.prompt)["change_plan"])
    facts = {fact.fact_id: fact for fact in candidate.facts}
    typed, removals = normalize_paragraph_edits(
        plan, tuple(typed), json.loads(model_input.prompt)
    )
    documents = assemble_paragraphs(plan, typed, model_input, facts)
    candidate = WrittenCandidate(
        documents,
        candidate.usage,
        candidate.relation_proposals,
        candidate.new_document_proposals,
        candidate.facts,
        typed,
        paragraph_copy_removals=removals,
    )
    candidate = validate_written_scope(plan, candidate, model_input)
    return replace(
        candidate,
        source_scope_extensions=collect_source_extensions(candidate, model_input),
    )


def assemble_written_candidate(snapshot, candidate, model_input):
    """파서를 우회한 포트와 보완 병합의 본문을 계획의 문단 조립 결과로 맞춘다."""
    if not isinstance(candidate, WrittenCandidate):
        plan_fail(
            "Written candidate with paragraph edits required",
            rule="plan.edits_complete",
        )
    plan = load_plan(json.loads(model_input.prompt)["change_plan"])
    # 최초 계획은 이미 검증했지만 어댑터·fake가 바꾼 문맥도 같은 기준으로 검사한다.
    validate_plan(snapshot, plan, model_input)
    facts = {fact.fact_id: fact for fact in candidate.facts}
    normalized, removals = normalize_paragraph_edits(
        plan, candidate.paragraph_edits, json.loads(model_input.prompt)
    )
    candidate = replace(
        candidate,
        document_proposals=assemble_paragraphs(plan, normalized, model_input, facts),
        paragraph_edits=normalized,
        paragraph_copy_removals=candidate.paragraph_copy_removals + removals,
    )
    candidate = validate_written_scope(plan, candidate, model_input)
    return candidate


def collect_source_extensions(candidate, model_input):
    """계획 마커 밖의 유효 원고 마커를 사용한 위치를 기록한다. 작업 범위는 바꾸지 않는다."""
    context = json.loads(model_input.prompt)
    plan = load_plan(context["change_plan"])
    markers = {
        facts_marker(context, item["marker"]): item["marker"]
        for item in context["evidence"]
    }
    facts = {fact.fact_id: fact for fact in candidate.facts}
    plans = {item.edit_id: item for item in plan.edits}
    scopes = [
        (
            edit.edit_id,
            plans[edit.edit_id].source_markers,
            tuple(source for key in edit.fact_ids for source in facts[key].evidence),
        )
        for edit in candidate.paragraph_edits
    ]
    new_plans = {item.candidate_id: item for item in plan.new_documents}
    scopes += [
        (item.candidate_id, new_plans[item.candidate_id].source_markers, item.evidence)
        for item in candidate.new_document_proposals
    ]
    relations = {
        tuple(sorted((item.document_id, item.target_document_id))): item
        for item in plan.relations
    }
    scopes += [
        ("|".join(pair), relations[pair].source_markers, item.evidence)
        for item in candidate.relation_proposals
        for pair in (tuple(sorted((item.document_id, item.target_document_id))),)
    ]
    return tuple(
        SourceScopeExtension(
            key, tuple(sorted(set(markers[e] for e in sources) - set(allowed)))
        )
        for key, allowed, sources in scopes
        if set(markers[e] for e in sources) - set(allowed)
    )


def normalize_paragraph_edits(plan, edits, context):
    """계획에 없는 edit_id는 버리고, 서버가 보존할 같은 문서 문단의 완전 복사본만 제거한다."""
    changed = {
        (item.document_id, item.paragraph_id)
        for item in plan.edits
        if item.operation in {"REPLACE", "DELETE"}
    }
    instructions = {item.edit_id: item for item in plan.edits}
    normalized, removals = [], []
    for edit in edits:
        operation = instructions.get(edit.edit_id)
        if operation is None:
            continue
        preserved = {
            p["text"]: p["paragraph_id"]
            for p in context["paragraphs"]
            if p["document_id"] == operation.document_id
            and (p["document_id"], p["paragraph_id"]) not in changed
            and p["text"]
        }
        copies = [
            (match, preserved[match.group()])
            for match in re.finditer(
                r"\S(?:.*?\S)?(?=\n[ \t]*\n|[ \t\r\n]*\Z)", edit.text, re.S
            )
            if match.group() in preserved
        ]
        text = edit.text
        for match, _ in reversed(copies):
            text = text[: match.start()] + text[match.end() :]
        if copies:
            text = text.strip("\r\n")
            removals.append(
                ParagraphCopyRemoval(
                    edit.edit_id, tuple(pid for _, pid in copies), edit.text
                )
            )
        normalized.append(replace(edit, text=text))
    return tuple(normalized), tuple(removals)
