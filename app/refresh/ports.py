"""Synchronous ports; adapters own IO and translate known failures.

RefreshFailure carries expected errors. Unexpected exceptions propagate. Request
identity includes selection settings as well as input content. Artifact writes
are immutable/idempotent per request, and conflicting data must be rejected.
"""

from typing import Protocol

from app.refresh.models import (
    ArtifactRef, Completion, DocumentBatch, ExecutionSnapshot, InputSnapshot,
    JobRecord, ModelCandidate, ModelInput, RefreshRequest, RefreshResult,
    RelatedDocuments,
)


class RefreshArtifactStore(Protocol):
    def read_input(self, request: RefreshRequest) -> InputSnapshot: ...
    def read_context(self, request: RefreshRequest) -> ExecutionSnapshot | None:
        """None only if never saved; expired/forbidden is an explicit failure."""
        ...
    def save_context(self, snapshot: ExecutionSnapshot) -> ArtifactRef: ...
    def read_result(self, request: RefreshRequest) -> RefreshResult | None: ...
    def save_result(self, request: RefreshRequest, result: RefreshResult) -> ArtifactRef: ...


class RelatedDocumentSource(Protocol):
    def find_related(self, request: RefreshRequest) -> RelatedDocuments:
        """Query explicit relations within scope/limits; report projection readiness."""
        ...


class DocumentSource(Protocol):
    def fetch_documents(self, project_id: str, document_ids: tuple[str, ...]) -> DocumentBatch:
        """Adapter enforces trusted scope; report missing IDs, not silent omission."""
        ...


class ProposalModel(Protocol):
    def generate(self, model_input: ModelInput) -> ModelCandidate: ...


class RefreshJobStore(Protocol):
    def claim(self, request: RefreshRequest, execution_id: str) -> JobRecord:
        """Atomically acquire available work; busy/conflict are explicit errors.

        Completed records may be read without taking ownership. Execution IDs are
        supplied by the delivery layer. Lease expiry/reclaim is an adapter concern.
        """
        ...
    def load(self, request: RefreshRequest) -> JobRecord | None: ...
    def checkpoint(self, record: JobRecord) -> None:
        """Only the current execution owner can advance/release the job."""
        ...


class RefreshCompletionPublisher(Protocol):
    def publish(self, completion: Completion) -> None:
        """May deliver then lose acknowledgment; consumers must deduplicate."""
        ...
