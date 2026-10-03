"""Internal values, not an S3 JSON schema or a Kafka/Content wire contract.

Tuples and frozen records keep snapshots immutable. IDs and revisions are supplied
by callers; authorization and business validation remain explicit rule boundaries.
"""

from dataclasses import dataclass
from enum import StrEnum


class DocumentState(StrEnum):
    ACTIVE = "ACTIVE"
    TRASHED = "TRASHED"
    DELETED = "DELETED"


class Readiness(StrEnum):
    READY = "READY"
    WAITING = "WAITING"
    UNKNOWN = "UNKNOWN"


class Outcome(StrEnum):
    PROPOSED = "PROPOSED"
    NO_CHANGE = "NO_CHANGE"
    FAILED = "FAILED"


class JobState(StrEnum):
    RUNNING = "RUNNING"
    RETRYABLE = "RETRYABLE"
    RESULT_READY = "RESULT_READY"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class ArtifactRef:
    bucket: str
    key: str


@dataclass(frozen=True)
class JobKey:
    project_id: str
    request_id: str
    input_fingerprint: str


@dataclass(frozen=True)
class SelectionSettings:
    relation_kinds: tuple[str, ...] = ()
    max_depth: int = 1
    max_documents: int = 10
    max_input_chars: int = 10000
    policy_version: str = "unimplemented"


@dataclass(frozen=True)
class RefreshRequest:
    job: JobKey
    input_ref: ArtifactRef
    seed_ids: tuple[str, ...]
    target_ids: tuple[str, ...] = ()
    discover_related: bool = True
    required_graph_version: str | None = None
    settings: SelectionSettings = SelectionSettings()


@dataclass(frozen=True)
class Relation:
    project_id: str
    source_id: str
    target_id: str
    kind: str


@dataclass(frozen=True)
class DocumentSnapshot:
    project_id: str
    document_id: str
    revision: int
    body: str
    properties: tuple[tuple[str, str], ...] = ()
    relations: tuple[Relation, ...] = ()
    state: DocumentState = DocumentState.ACTIVE


@dataclass(frozen=True)
class InputSnapshot:
    job: JobKey
    documents: tuple[DocumentSnapshot, ...]


@dataclass(frozen=True)
class RelatedDocuments:
    document_ids: tuple[str, ...] = ()
    relations: tuple[Relation, ...] = ()
    readiness: Readiness = Readiness.READY
    observed_version: str | None = None


@dataclass(frozen=True)
class DocumentBatch:
    documents: tuple[DocumentSnapshot, ...]
    missing_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Selection:
    document_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionSnapshot:
    request: RefreshRequest
    documents: tuple[DocumentSnapshot, ...]
    related: RelatedDocuments


@dataclass(frozen=True)
class Evidence:
    document_id: str
    revision: int
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class Chunk:
    evidence: Evidence


@dataclass(frozen=True)
class Proposal:
    document_id: str
    base_revision: int
    field: str
    value: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ModelInput:
    prompt: str
    schema_version: str
    prompt_version: str
    model: str


@dataclass(frozen=True)
class ModelCandidate:
    proposals: tuple[Proposal, ...]
    usage: Usage = Usage()


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class RefreshResult:
    job: JobKey
    outcome: Outcome
    proposals: tuple[Proposal, ...] = ()
    usage: Usage = Usage()
    failure: Failure | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class Completion:
    job: JobKey
    result_ref: ArtifactRef
    outcome: Outcome
    failure: Failure | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class JobRecord:
    request: RefreshRequest
    execution_id: str
    state: JobState = JobState.RUNNING
    context_ref: ArtifactRef | None = None
    result_ref: ArtifactRef | None = None
    failure: Failure | None = None
