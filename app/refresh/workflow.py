"""문단 ID 계획 검증·작성 문맥·본문 조립. 원고·Content 문서를 직접 변경하지 않는다."""

import json
import re
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher

from app.refresh.errors import RefreshFailure
from app.refresh.evidence import unique_evidence
from app.refresh.facts import fact_description
from app.refresh.models import (
    ChangePlan,
    DocumentProposal,
    Failure,
    ParagraphCopyRemoval,
    ParagraphEdit,
    PlannedRelation,
    RelationProposal,
    SourceScopeExtension,
    Usage,
    ValidationDiagnostic,
    WrittenCandidate,
)
from app.refresh.policy import RELATION_KEYS
from app.refresh.tolerance import resolve_id, strip_editorial

PARAGRAPH = re.compile(r"\S(?:[^\n]*\S)?")
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
    """줄마다 원문 위치를 보존하여 대상 설정에만 문단 ID를 붙인다.

    Content는 에디터의 블록을 줄바꿈 하나로 이어 평문을 만든다. 그래서 한 줄이 한 문단이다.
    """
    result = []
    bodies = current_bodies(snapshot)
    for document in snapshot.documents:
        if document.document_id not in snapshot.target_ids:
            continue
        body = bodies[document.document_id]
        matches = list(PARAGRAPH.finditer(body))
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
    """계획을 실행할 수 있는 범위로 맞춘다. 의미상 완전성은 보증하지 않는다.

    잘못된 작업·신규 설정·관계는 실패 대신 버리거나 고친다. 같은 문단의 작업은 하나로 합치고,
    옮겨 적다 틀린 문서 ID는 가장 가까운 실제 ID로 고치며, 방향만 다른 관계는 하나로 합친다.
    신규 설정과 그 근거 원고의 관계가 빠졌으면 채운다.
    """
    from app.refresh.validation import existing_setting_names, setting_name

    if not isinstance(plan, ChangePlan):
        plan_fail("Typed change plan required")
    context = json.loads(model_input.prompt)
    markers = {item["marker"]: item for item in context["evidence"]}
    inventory = {(p.document_id, p.paragraph_id): p for p in paragraphs(snapshot)}
    existing = {d.document_id: d for d in snapshot.documents}
    targets = set(snapshot.target_ids)

    def sources(item):
        return tuple(
            dict.fromkeys(
                marker
                for marker in item.source_markers
                if marker in markers
                and markers[marker]["document_id"] in snapshot.changed_ids
            )
        )

    edits = {}
    if context["allow_document_updates"]:
        for edit in plan.edits:
            document_id = resolve_id(edit.document_id, targets)
            operation = "APPEND" if edit.paragraph_id == "P0000" else edit.operation
            paragraph = inventory.get((document_id, edit.paragraph_id))
            found = sources(edit)
            if (
                paragraph is None
                or operation not in {"REPLACE", "DELETE", "APPEND"}
                or not found
                or not isinstance(edit.instruction, str)
                or not edit.instruction.strip()
            ):
                continue
            position = (
                document_id,
                edit.paragraph_id,
                "APPEND" if operation == "APPEND" else "CHANGE",
            )
            previous = edits.get(position)
            if previous is None:
                edits[position] = replace(
                    edit,
                    document_id=document_id,
                    base_revision_no=paragraph.base_revision_no,
                    operation=operation,
                    instruction=edit.instruction[:2000],
                    source_markers=found,
                )
                continue
            edits[position] = replace(
                previous,
                operation=previous.operation
                if previous.operation == operation
                else "REPLACE",
                instruction=(previous.instruction + "\n" + edit.instruction)[:2000],
                source_markers=tuple(dict.fromkeys(previous.source_markers + found)),
            )
    planned_edits, edit_ids = [], set()
    for index, edit in enumerate(list(edits.values())[: len(inventory) * 2], 1):
        edit_id = edit.edit_id
        if not _bounded(edit_id, 200) or edit_id in edit_ids:
            edit_id = next(
                f"E{index}-{n}"
                for n in range(1, 1000)
                if f"E{index}-{n}" not in edit_ids
            )
        edit_ids.add(edit_id)
        planned_edits.append(replace(edit, edit_id=edit_id))
    existing_names = existing_setting_names(snapshot)
    new_documents, renamed, names = {}, {}, set()
    if context["allow_new_documents"]:
        for item in plan.new_documents:
            name = item.name.strip() if isinstance(item.name, str) else ""
            candidate_id = f"new:{item.folder_code}:{name}"
            key = (item.folder_code, setting_name(name))
            found = sources(item)
            if (
                not name
                or len(name) > 200
                or item.folder_code not in RELATION_KEYS
                or item.folder_code == "MANUSCRIPT"
                or not isinstance(item.instruction, str)
                or not item.instruction.strip()
                or not found
                or key in existing_names
                or key in names
                or candidate_id in existing
                or len(new_documents) >= 20
            ):
                continue
            renamed[item.candidate_id] = candidate_id
            names.add(key)
            new_documents[candidate_id] = replace(
                item,
                candidate_id=candidate_id,
                name=name,
                instruction=item.instruction[:2000],
                source_markers=found,
            )
    scope = context["relation_scope"]
    allowed_pairs = {tuple(sorted(pair)) for pair in scope["allowed_existing_pairs"]}
    known = set(existing) | set(new_documents)
    relations = {}
    for item in plan.relations:
        source = resolve_id(renamed.get(item.document_id, item.document_id), known)
        target = resolve_id(
            renamed.get(item.target_document_id, item.target_document_id), known
        )
        found = sources(item)
        if source is None or target is None or source == target or not found:
            continue
        pair = tuple(sorted((source, target)))
        new_endpoint = bool(set(pair) & set(new_documents))
        if (new_endpoint and not scope["allow_new_endpoint_relations"]) or (
            not new_endpoint and pair not in allowed_pairs
        ):
            continue
        previous = relations.get(pair)
        relations[pair] = (
            PlannedRelation(source, target, found)
            if previous is None
            else replace(
                previous,
                source_markers=tuple(dict.fromkeys(previous.source_markers + found)),
            )
        )
    if scope["allow_new_endpoint_relations"]:
        for item in new_documents.values():
            manuscripts = {}
            for marker in item.source_markers:
                document_id = markers[marker]["document_id"]
                if existing[document_id].folder_code == "MANUSCRIPT":
                    manuscripts.setdefault(document_id, []).append(marker)
            for document_id, found in manuscripts.items():
                pair = tuple(sorted((document_id, item.candidate_id)))
                if pair not in relations:
                    relations[pair] = PlannedRelation(
                        document_id, item.candidate_id, tuple(found)
                    )
    return replace(
        plan,
        reviewed_document_ids=tuple(snapshot.target_ids),
        edits=tuple(planned_edits),
        new_documents=tuple(new_documents.values()),
        relations=tuple(relations.values())[:100],
    )


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
    """쓸 수 있는 문단 작업만 조립하고 나머지 원문은 그대로 둔다.

    빠진 작업, 현재 변경 원고의 근거 사실이 없는 작업, 내용이 바뀌지 않은 작업은 그 문단을
    원문대로 둔다. 작성 지시가 본문 문장으로 옮겨진 경우 그 문장은 지운다.
    """
    context = json.loads(model_input.prompt)
    inventory = {
        (p["document_id"], p["paragraph_id"]): p for p in context["paragraphs"]
    }
    documents = {item["document_id"]: item for item in context["documents"]}
    indexed = {}
    for edit in edits:
        indexed.setdefault(edit.edit_id, edit)
    allowed = {
        facts_marker(context, item["marker"])
        for item in context["evidence"]
        if item["document_id"] in context["changed_ids"]
    }
    grouped = {}
    for operation in plan.edits:
        edit = indexed.get(operation.edit_id)
        paragraph = inventory.get((operation.document_id, operation.paragraph_id))
        if edit is None or paragraph is None:
            continue
        refs = tuple(
            key
            for key in dict.fromkeys(edit.fact_ids)
            if key in facts
            and facts[key].evidence
            and facts[key].evidence[0] in allowed
        )
        text = "" if operation.operation == "DELETE" else strip_editorial(edit.text)
        if (
            not refs
            or (operation.operation != "DELETE" and not text.strip())
            or len(text) > 5000
            or (operation.operation == "REPLACE" and text == paragraph["text"])
        ):
            continue
        grouped.setdefault(operation.document_id, []).append(
            (operation, replace(edit, text=text, fact_ids=refs), paragraph)
        )
    result = []
    for document_id, operations in sorted(grouped.items()):
        body = context["settings_bodies"][document_id]
        separator = "\n\n" if "\n\n" in body.strip() else "\n"
        changes = {}
        appends = {}
        for operation, edit, paragraph in operations:
            if operation.operation == "APPEND":
                appends[paragraph["end"]] = (
                    separator if paragraph["end"] else ""
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
            continue
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


def _distinct_statements(refs, facts):
    """문장이 거의 같은 사실은 앞의 것 하나만 남긴다. 관계 설명이 같은 말을 반복하지 않게 한다."""
    kept, seen = [], []
    for key in refs:
        text = re.sub(r"[\s\W_]+", "", facts[key].statement)
        if any(SequenceMatcher(None, text, other).ratio() >= 0.9 for other in seen):
            continue
        kept.append(key)
        seen.append(text)
    return tuple(kept)


def validate_written_scope(plan, candidate, model_input):
    """신규 설정과 관계를 계획 범위로 맞춘다.

    계획에 없는 신규 설정·관계는 버리고 빠진 것은 제안하지 않는다. 신규 설정의 근거에는 참조한
    사실의 출처를 더하고, 작성 지시가 옮겨진 문장은 지운다. 같은 쌍의 관계는 하나로 합치고 거의
    같은 사실은 한 번만 참조하며, 관계 키와 기준 revision은 끝점의 유형·revision으로 채운다.
    계획한 원고와 신규 설정의 관계를 작성 단계가 빠뜨렸으면 신규 설정의 사실로 만든다.
    """
    context = json.loads(model_input.prompt)
    allowed = {
        facts_marker(context, item["marker"])
        for item in context["evidence"]
        if item["document_id"] in context["changed_ids"]
    }
    facts = {fact.fact_id: fact for fact in candidate.facts}

    def usable(refs):
        return tuple(
            key
            for key in dict.fromkeys(refs)
            if key in facts and all(e in allowed for e in facts[key].evidence)
        )

    planned_new = {item.candidate_id: item for item in plan.new_documents}
    new = {}
    for proposal in candidate.new_document_proposals:
        item = planned_new.get(resolve_id(proposal.candidate_id, set(planned_new)))
        refs = usable(proposal.fact_ids)
        body = strip_editorial(proposal.body_text)
        if item is None or item.candidate_id in new or not refs or not body.strip():
            continue
        new[item.candidate_id] = replace(
            proposal,
            candidate_id=item.candidate_id,
            folder_code=item.folder_code,
            name=item.name,
            body_text=body,
            evidence=unique_evidence(
                tuple(e for e in proposal.evidence if e in allowed)
                + tuple(e for key in refs for e in facts[key].evidence)
            ),
            fact_ids=refs,
        )
    endpoints = {
        item["document_id"]: (item["folder_code"], item["revision_no"])
        for item in context["documents"]
    }
    endpoints.update({key: (item.folder_code, None) for key, item in new.items()})
    planned = {
        tuple(sorted((p.document_id, p.target_document_id))) for p in plan.relations
    }

    def relation_for(source, target, refs):
        selected = [facts[key] for key in refs]
        return RelationProposal(
            source,
            target,
            endpoints[source][1],
            endpoints[target][1],
            RELATION_KEYS.get(endpoints[target][0]),
            RELATION_KEYS.get(endpoints[source][0]),
            fact_description(selected),
            unique_evidence(e for fact in selected for e in fact.evidence),
            "ADD",
            refs,
        )

    relation = {}
    for proposal in candidate.relation_proposals:
        source = resolve_id(proposal.document_id, set(endpoints))
        target = resolve_id(proposal.target_document_id, set(endpoints))
        if source is None or target is None:
            continue
        pair = tuple(sorted((source, target)))
        if pair not in planned:
            continue
        previous = relation.get(pair)
        if previous is not None:
            source, target = previous.document_id, previous.target_document_id
        refs = _distinct_statements(
            usable((previous.fact_ids if previous else ()) + proposal.fact_ids), facts
        )
        if refs:
            relation[pair] = relation_for(source, target, refs)
    for pair in planned - set(relation):
        manuscript = next(
            (key for key in pair if endpoints.get(key, ("",))[0] == "MANUSCRIPT"), None
        )
        other = pair[1] if manuscript == pair[0] else pair[0]
        if manuscript is None or other not in new:
            continue
        refs = _distinct_statements(
            tuple(
                key
                for key in new[other].fact_ids
                if any(e.document_id == manuscript for e in facts[key].evidence)
            ),
            facts,
        )
        if refs:
            relation[pair] = relation_for(other, manuscript, refs)
    return replace(
        candidate,
        new_document_proposals=tuple(new.values()),
        relation_proposals=tuple(relation.values()),
    )


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
    typed = [
        ParagraphEdit(
            edit["edit_id"],
            edit["text"],
            tuple(
                dict.fromkeys(
                    fact_refs[key] for key in edit["fact_ids"] if key in fact_refs
                )
            ),
        )
        for edit in edits
    ]
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
            for match in PARAGRAPH.finditer(edit.text)
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
