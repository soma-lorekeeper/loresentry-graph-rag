"""Business boundaries only. Every default deliberately fails closed.

Tests may explicitly inject a subclass with scripted answers. No test stub is
imported here and no default silently accepts input or produces a proposal.
"""

from app.refresh.errors import RulesNotImplemented
from app.refresh.models import (
    Chunk, DocumentBatch, ExecutionSnapshot, InputSnapshot, ModelCandidate,
    ModelInput, RefreshRequest, RefreshResult, RelatedDocuments, Selection,
)


class RefreshRules:
    def validate_input(self, request: RefreshRequest, source: InputSnapshot) -> None:
        raise RulesNotImplemented("validate_input")

    def select(self, request: RefreshRequest, source: InputSnapshot,
               related: RelatedDocuments) -> Selection:
        raise RulesNotImplemented("select")

    def assemble(self, request: RefreshRequest, source: InputSnapshot,
                 related: RelatedDocuments, selection: Selection,
                 fetched: DocumentBatch) -> ExecutionSnapshot:
        """Validate scope/state/completeness and keep original snapshot revisions."""
        raise RulesNotImplemented("assemble")

    def chunk(self, snapshot: ExecutionSnapshot) -> tuple[Chunk, ...]:
        raise RulesNotImplemented("chunk")

    def build_context(self, snapshot: ExecutionSnapshot, chunks: tuple[Chunk, ...]) -> str:
        raise RulesNotImplemented("build_context")

    def build_prompt(self, context: str) -> ModelInput:
        raise RulesNotImplemented("build_prompt")

    def validate_candidate(self, snapshot: ExecutionSnapshot, candidate: ModelCandidate,
                           model_input: ModelInput) -> RefreshResult:
        raise RulesNotImplemented("validate_candidate")
