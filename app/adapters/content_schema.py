"""Content 문서를 검증하고 분석용 스냅샷으로 변환한다.

S3 최신화 입력(refresh-input-v2)의 문서와 목록 조회 API의 문서는 같은 모양이라 한 모델로 검증한다.
"""

from typing import Annotated, Self
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


class ContentProperty(ResponseObject):
    key: str = Field(min_length=1)
    value: str


class ContentRelation(ResponseObject):
    target_document_id: str = Field(min_length=1)
    relation_key: str
    description: str


class ContentDocument(ResponseObject):
    """서식을 뺀 본문과 속성·관계를 가진 Content 문서 하나. 항상 ACTIVE 문서다.

    문서 ID의 UUID 형식은 여기서 보지 않는다. S3 입력은 이벤트 manifest의 정규 UUID와,
    목록 응답은 요청한 ID와 정확히 대조되고, 관계 대상은 추가 조회 전에 다시 검사된다.
    """

    id: str = Field(min_length=1)
    revision_no: int = Field(ge=0)
    folder_code: str
    title: str | None = Field(default=None, min_length=1)
    body_text: str
    properties: list[ContentProperty]
    relations: list[ContentRelation]

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        if self.folder_code not in RELATION_KEYS:
            raise ValueError("Unsupported document category")
        # 속성 키는 반복될 수 있다(alias 여러 개 등). Content에도 unique 제약이 없다.
        if any(not item.key.strip() for item in self.properties):
            raise ValueError("Property keys must be nonblank")
        targets = [item.target_document_id for item in self.relations]
        if len(targets) != len(set(targets)) or self.id in targets:
            raise ValueError("Relations must be unique and cannot refer to self")
        if any(
            item.relation_key not in RELATION_KEYS.values() for item in self.relations
        ):
            raise ValueError("Unsupported relation category")
        return self

    def snapshot(self, project_id: str) -> DocumentSnapshot:
        """요청 프로젝트의 스냅샷으로 바꾼다. 실린 관계의 주인은 이 문서다."""
        return DocumentSnapshot(
            project_id=project_id,
            document_id=self.id,
            revision_no=self.revision_no,
            body_text=self.body_text,
            properties=tuple((item.key, item.value) for item in self.properties),
            relations=tuple(
                Relation(
                    project_id,
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


class ContentsResponse(ResponseObject):
    """GET /projects/{id}/files/content 응답. missing은 휴지통·없음·다른 프로젝트 문서다."""

    project_id: Identifier
    documents: list[ContentDocument]
    missing: list[Identifier]
