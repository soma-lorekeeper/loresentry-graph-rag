"""Refresh orchestration skeleton. All IO is injected; business rules are pending.

This is not registered as a production endpoint/consumer. Delivery owns execution
IDs, retry budgets and lease recovery. There are no implicit retries here.
"""

from dataclasses import replace

from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import (
    Completion, DocumentBatch, Failure, JobState, Outcome, Readiness,
    RefreshRequest, RefreshResult, RelatedDocuments,
)
from app.refresh.ports import (
    DocumentSource, ProposalModel, RefreshArtifactStore, RefreshCompletionPublisher,
    RefreshJobStore, RelatedDocumentSource,
)
from app.refresh.rules import RefreshRules


class RefreshService:
    def __init__(self, *, artifacts: RefreshArtifactStore, relations: RelatedDocumentSource,
                 documents: DocumentSource, model: ProposalModel, jobs: RefreshJobStore,
                 publisher: RefreshCompletionPublisher, rules: RefreshRules | None = None):
        self.artifacts = artifacts
        self.relations = relations
        self.documents = documents
        self.model = model
        self.jobs = jobs
        self.publisher = publisher
        self.rules = rules if rules is not None else RefreshRules()

    def run(self, request: RefreshRequest, execution_id: str) -> Completion:
        # A rejected claim grants no ownership, so it must not release another job.
        record = self.jobs.claim(request, execution_id)
        try:
            result = self.artifacts.read_result(request)
            if record.state == JobState.COMPLETED:
                if result is None or record.result_ref is None:
                    raise RefreshFailure(Failure("RESULT_MISSING", "Completed result unavailable"))
                return self._completion(request, record.result_ref, result)
            if record.result_ref is not None and result is None:
                raise RefreshFailure(Failure("RESULT_MISSING", "Saved result unavailable"))
            if result is None:
                try:
                    result = self._compute(request, record)
                except RefreshFailure as error:
                    if error.failure.retryable:
                        raise
                    result = RefreshResult(request.job, Outcome.FAILED, failure=error.failure)
            # Idempotent write also recovers a result saved before a job checkpoint.
            if result.job != request.job:
                raise RequestConflict("result identity differs")
            result_ref = self.artifacts.save_result(request, result)
            # _compute may have checkpointed a context reference.
            current = self.jobs.load(request)
            if current is None or current.execution_id != execution_id:
                raise RequestConflict("job owner changed during execution")
            record = replace(current, state=JobState.RESULT_READY, result_ref=result_ref,
                             failure=result.failure)
            self.jobs.checkpoint(record)
            completion = self._completion(request, result_ref, result)
            self.publisher.publish(completion)
            self.jobs.checkpoint(replace(record, state=JobState.COMPLETED))
            return completion
        except Exception as error:
            # Release known ownership only. A failed release remains visible for
            # delivery/lease recovery; it must not mask the original failure.
            try:
                current = self.jobs.load(request)
                if (current is not None and current.execution_id == execution_id
                        and current.state not in {JobState.COMPLETED, JobState.RETRYABLE}):
                    failure = error.failure if isinstance(error, RefreshFailure) else None
                    self.jobs.checkpoint(replace(current, state=JobState.RETRYABLE, failure=failure))
            except Exception as release_error:
                error.add_note(f"Job release failed: {type(release_error).__name__}")
            raise

    def _compute(self, request, record):
        snapshot = self.artifacts.read_context(request)
        if snapshot is None:
            if record.context_ref is not None:
                raise RefreshFailure(Failure("CONTEXT_MISSING", "Saved execution snapshot unavailable"))
            source = self.artifacts.read_input(request)
            self.rules.validate_input(request, source)
            related = RelatedDocuments()
            if request.discover_related:
                related = self.relations.find_related(request)
                if related.readiness != Readiness.READY:
                    raise RefreshFailure(Failure("GRAPH_NOT_READY", "Relationship projection not ready", True))
            selection = self.rules.select(request, source, related)
            fetched = DocumentBatch(())
            if selection.missing_ids:
                fetched = self.documents.fetch_documents(request.job.project_id, selection.missing_ids)
            snapshot = self.rules.assemble(request, source, related, selection, fetched)
        if snapshot.request != request:
            raise RequestConflict("execution snapshot belongs to a different request")
        context_ref = self.artifacts.save_context(snapshot)
        self.jobs.checkpoint(replace(record, context_ref=context_ref))
        chunks = self.rules.chunk(snapshot)
        context = self.rules.build_context(snapshot, chunks)
        model_input = self.rules.build_prompt(context)
        candidate = self.model.generate(model_input)
        return self.rules.validate_candidate(snapshot, candidate, model_input)

    @staticmethod
    def _completion(request, result_ref, result):
        if result.job != request.job:
            raise RequestConflict("result identity differs")
        return Completion(request.job, result_ref, result.outcome, result.failure, result.prompt_version)
