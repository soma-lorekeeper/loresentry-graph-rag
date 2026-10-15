"""Content 조회 응답을 검증하고 분석용 스냅샷으로 변환한다."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.refresh.models import DocumentSnapshot, Relation
from app.refresh.policy import RELATION_KEYS


def canonical_id(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("A canonical UUID is required")
    return value


Identifier = Annotated[str, AfterValidator(canonical_id)]


class ResponseObject(BaseModel):
    # 사용하는 필드는 엄격히 검사하되 Content의 부가 필드 추가는 허용한다.
    model_config = ConfigDict(strict=True, extra="ignore")


class EditorNode(ResponseObject):
    type: Literal[
        "doc",
        "paragraph",
        "text",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "blockquote",
        "codeBlock",
        "horizontalRule",
        "hardBreak",
    ]
    text: str | None = None
    content: list[Self] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_text(self) -> Self:
        if self.type == "text" and self.text is None:
            raise ValueError("Text nodes require text")
        return self


class EditorBody(ResponseObject):
    schema_version: int = Field(ge=1, le=1)
    doc: EditorNode

    @model_validator(mode="after")
    def require_root(self) -> Self:
        if self.doc.type != "doc":
            raise ValueError("Editor root must be a doc")
        return self


def body_text(body: EditorBody) -> str:
    """Content BodyText.extract와 같은 블록·목록·hardBreak 규칙. 공백은 보존한다."""

    def inline(node: EditorNode) -> str:
        if node.type == "text":
            return node.text
        if node.type == "hardBreak":
            return "\n"
        separator = (
            "\n"
            if node.type in {"bulletList", "orderedList", "blockquote", "listItem"}
            else ""
        )
        return separator.join(inline(child) for child in node.content)

    parts = []
    for block in body.doc.content:
        if parts:
            parts.append("\n")
        text = inline(block)
        if text:
            parts.append(text)
    return "".join(parts)


class TextProperty(ResponseObject):
    key: str = Field(min_length=1)
    value: str


class ContentRelation(ResponseObject):
    relation_key: str
    target_document_id: Identifier
    description: str


class ProjectResponse(ResponseObject):
    id: Identifier
    trashed_at: str | None


class ContentResponse(ResponseObject):
    id: Identifier
    project_id: Identifier
    title: str = Field(min_length=1)
    folder_code: str
    revision_no: int = Field(ge=0)
    body: EditorBody | None
    legacy_body_md: str | None
    properties: list[TextProperty]
    relations: list[ContentRelation]

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        if self.folder_code not in RELATION_KEYS:
            raise ValueError("Unsupported document category")
        if (self.body is None) == (self.legacy_body_md is None):
            raise ValueError("Exactly one body representation is required")
        keys = [item.key for item in self.properties]
        if len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
            raise ValueError("Property keys must be unique and nonblank")
        targets = [item.target_document_id for item in self.relations]
        if len(targets) != len(set(targets)) or self.id in targets:
            raise ValueError("Relations must be unique and cannot refer to self")
        if any(
            item.relation_key not in RELATION_KEYS.values() for item in self.relations
        ):
            raise ValueError("Unsupported relation category")
        return self

    def snapshot(self) -> DocumentSnapshot:
        return DocumentSnapshot(
            project_id=self.project_id,
            document_id=self.id,
            revision_no=self.revision_no,
            body_text=body_text(self.body)
            if self.body is not None
            else self.legacy_body_md,
            properties=tuple((item.key, item.value) for item in self.properties),
            relations=tuple(
                Relation(
                    self.project_id,
                    self.id,
                    item.target_document_id,
                    item.relation_key,
                    item.description,
                )
                for item in self.relations
            ),
            folder_code=self.folder_code,
            title=self.title,
        )
