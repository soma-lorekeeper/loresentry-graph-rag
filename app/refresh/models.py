"""갱신안 처리에 사용하는 내부 값. S3 JSON·Kafka·Content의 전송 스키마는 아니다.

frozen 데이터 클래스와 tuple 필드로 스냅샷을 표현한다. 생성자 자체는 입력의
권한·범위·revision 유효성을 검증하지 않는다. ID와 revision은 호출자가 전달한다.
"""

from dataclasses import dataclass
from enum import StrEnum


class DocumentState(StrEnum):
    """문서 조회 시 전달받은 활성·휴지통·삭제 상태."""

    ACTIVE = "ACTIVE"
    TRASHED = "TRASHED"
    DELETED = "DELETED"


class Readiness(StrEnum):
    """요청에 필요한 관계 그래프 반영 상태. UNKNOWN도 준비 완료로 취급하지 않는다."""

    READY = "READY"
    WAITING = "WAITING"
    UNKNOWN = "UNKNOWN"


class Outcome(StrEnum):
    """갱신안 있음, 변경 없음, 실패로 구분하는 처리 결과."""

    PROPOSED = "PROPOSED"
    NO_CHANGE = "NO_CHANGE"
    FAILED = "FAILED"


class JobState(StrEnum):
    """작업 소유권과 결과 저장·발행 진행을 복구하기 위한 상태."""

    RUNNING = "RUNNING"
    RETRYABLE = "RETRYABLE"
    RESULT_READY = "RESULT_READY"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class ArtifactRef:
    """저장된 입력·실행 스냅샷·결과를 찾기 위한 버킷과 객체 키."""

    bucket: str
    key: str


@dataclass(frozen=True)
class JobKey:
    """프로젝트 안에서 요청과 입력을 구분하는 식별 값.

    input_fingerprint는 호출자가 제공한다. 이 데이터 클래스가 해시를 계산하지 않는다.
    """

    project_id: str
    request_id: str
    input_fingerprint: str


@dataclass(frozen=True)
class SelectionSettings:
    """문서 선택 정책에 전달할 관계 종류·탐색 깊이·입력 크기 제한.

    기본 policy_version은 미구현을 뜻한다. 값 생성만으로 제한이 적용되지는 않는다.
    """

    relation_kinds: tuple[str, ...] = ()
    max_depth: int = 1
    max_documents: int = 10
    max_input_chars: int = 10000
    policy_version: str = "unimplemented"


@dataclass(frozen=True)
class RefreshRequest:
    """입력 위치와 갱신안 생성 범위를 전달하는 요청.

    Attributes:
        seed_ids: 관련 문서 탐색의 시작 문서 ID.
        target_ids: 요청에서 지정한 갱신 대상 ID.
        discover_related: 관계 조회를 수행할지 여부.
        required_graph_version: 관계 조회 구현에 전달할 반영 버전 요구값.

    대상 선택과 버전 비교의 구체적 정책은 이 데이터 클래스가 결정하지 않는다.
    """

    job: JobKey
    input_ref: ArtifactRef
    seed_ids: tuple[str, ...]
    target_ids: tuple[str, ...] = ()
    discover_related: bool = True
    required_graph_version: str | None = None
    settings: SelectionSettings = SelectionSettings()


@dataclass(frozen=True)
class Relation:
    """프로젝트 안의 출발 문서·도착 문서·관계 종류로 표현한 명시적 관계."""

    project_id: str
    source_id: str
    target_id: str
    kind: str


@dataclass(frozen=True)
class DocumentSnapshot:
    """조회 시점의 문서 본문·속성·관계·상태와 해당 revision."""

    project_id: str
    document_id: str
    revision: int
    body: str
    properties: tuple[tuple[str, str], ...] = ()
    relations: tuple[Relation, ...] = ()
    state: DocumentState = DocumentState.ACTIVE


@dataclass(frozen=True)
class InputSnapshot:
    """한 요청의 입력 객체에서 읽은 문서 스냅샷 묶음."""

    job: JobKey
    documents: tuple[DocumentSnapshot, ...]


@dataclass(frozen=True)
class RelatedDocuments:
    """관계 조회로 얻은 문서 ID·관계와 관측 버전·반영 준비 상태."""

    document_ids: tuple[str, ...] = ()
    relations: tuple[Relation, ...] = ()
    readiness: Readiness = Readiness.READY
    observed_version: str | None = None


@dataclass(frozen=True)
class DocumentBatch:
    """추가 조회에서 얻은 문서와 얻지 못한 문서 ID를 구분한 결과."""

    documents: tuple[DocumentSnapshot, ...]
    missing_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Selection:
    """판단 계층이 선택한 문서 ID와 추가 본문 조회가 필요한 ID."""

    document_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionSnapshot:
    """같은 입력으로 재실행하기 위해 확정·저장하는 요청과 문서·관계 묶음."""

    request: RefreshRequest
    documents: tuple[DocumentSnapshot, ...]
    related: RelatedDocuments


@dataclass(frozen=True)
class Evidence:
    """후보의 근거가 되는 문서 revision, 본문 위치와 인용문.

    start·end의 해석과 quote 일치 여부는 분할·검증 규칙에서 정의하고 확인해야 한다.
    이 데이터 클래스는 위치 범위나 인용문을 검증하지 않는다.
    """

    document_id: str
    revision: int
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class Chunk:
    """출처 근거와 함께 모델 문맥 구성에 사용할 본문 조각."""

    evidence: Evidence


@dataclass(frozen=True)
class Proposal:
    """기준 revision의 문서 필드에 제안하는 값과 근거. 생성만으로 문서를 변경하지 않는다."""

    document_id: str
    base_revision: int
    field: str
    value: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Usage:
    """모델 호출 및 입출력 토큰 사용량을 전달하는 값."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ModelInput:
    """외부 모델 호출에 사용할 프롬프트, 스키마·프롬프트 버전과 모델 식별자."""

    prompt: str
    schema_version: str
    prompt_version: str
    model: str


@dataclass(frozen=True)
class ModelCandidate:
    """모델이 반환한 갱신안 후보와 사용량. 아직 업무 검증을 통과하지 않은 값."""

    proposals: tuple[Proposal, ...]
    usage: Usage = Usage()


@dataclass(frozen=True)
class Failure:
    """예상 가능한 실패의 코드·메시지와 재시도 가능 여부.

    retryable은 실패 분류이며, 이 값이 자동으로 재시도를 실행하지 않는다.
    """

    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class RefreshResult:
    """요청별 처리 결과와 갱신안·사용량·실패 정보. 결과 저장소에 보존할 값."""

    job: JobKey
    outcome: Outcome
    proposals: tuple[Proposal, ...] = ()
    usage: Usage = Usage()
    failure: Failure | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class Completion:
    """완료 발행에 사용하는 결과 참조와 처리 결과. 본문 전체를 담지 않는다."""

    job: JobKey
    result_ref: ArtifactRef
    outcome: Outcome
    failure: Failure | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class JobRecord:
    """현재 실행 소유자, 진행 상태, 재개할 객체 참조와 실패 정보를 기록하는 값."""

    request: RefreshRequest
    execution_id: str
    state: JobState = JobState.RUNNING
    context_ref: ArtifactRef | None = None
    result_ref: ArtifactRef | None = None
    failure: Failure | None = None
