"""Deterministic, in-memory IO substitutes; no network or durability guarantees."""

from collections import defaultdict, deque
from copy import deepcopy

from app.refresh.errors import RefreshFailure, RequestConflict
from app.refresh.models import (
    ArtifactRef, DocumentBatch, ExecutionSnapshot, Failure, InputSnapshot,
    RefreshRequest, RefreshResult, RelatedDocuments,
)


class Calls:
    """Shared call trace and per-operation scripted exceptions (None = success)."""

    def __init__(self):
        self.events = []
        self.failures = defaultdict(deque)

    def record(self, name, *args):
        self.events.append((name, deepcopy(args)))
        if self.failures[name]:
            failure = self.failures[name].popleft()
            if failure is not None:
                raise failure

    def names(self):
        return [name for name, _ in self.events]


def identity(request):
    return request.job.project_id, request.job.request_id


class FakeArtifacts:
    def __init__(self, inputs=None, calls=None):
        self.calls = calls if calls is not None else Calls()
        self.inputs = deepcopy(inputs or {})
        self.contexts = {}
        self.results = {}
        self.requests = {}

    def _check(self, request):
        key = identity(request)
        prior = self.requests.get(key)
        if prior is not None and prior != request:
            raise RequestConflict("artifact request identity differs")
        return key

    def read_input(self, request: RefreshRequest) -> InputSnapshot:
        self.calls.record("artifacts.read_input", request)
        self._check(request)
        if request.input_ref not in self.inputs:
            raise RefreshFailure(Failure("INPUT_MISSING", "Input unavailable"))
        source = self.inputs[request.input_ref]
        if source.job != request.job:
            raise RequestConflict("input fingerprint or scope differs")
        return deepcopy(source)

    def read_context(self, request: RefreshRequest) -> ExecutionSnapshot | None:
        self.calls.record("artifacts.read_context", request)
        return deepcopy(self.contexts.get(self._check(request)))

    def save_context(self, snapshot: ExecutionSnapshot) -> ArtifactRef:
        self.calls.record("artifacts.save_context", snapshot)
        key = self._check(snapshot.request)
        if key in self.contexts and self.contexts[key] != snapshot:
            raise RequestConflict("execution snapshot is immutable")
        self.requests[key] = snapshot.request
        self.contexts[key] = deepcopy(snapshot)
        return self._ref(snapshot.request, "context")

    def read_result(self, request: RefreshRequest) -> RefreshResult | None:
        self.calls.record("artifacts.read_result", request)
        return deepcopy(self.results.get(self._check(request)))

    def save_result(self, request: RefreshRequest, result: RefreshResult) -> ArtifactRef:
        self.calls.record("artifacts.save_result", request, result)
        key = self._check(request)
        if result.job != request.job:
            raise RequestConflict("result identity differs")
        if key in self.results and self.results[key] != result:
            raise RequestConflict("saved result is immutable")
        self.requests[key] = request
        self.results[key] = deepcopy(result)
        return self._ref(request, "result")

    @staticmethod
    def _ref(request, kind):
        return ArtifactRef("fake-artifacts", f"{request.job.project_id}/{request.job.request_id}/{kind}")


class FakeRelations:
    def __init__(self, response: RelatedDocuments, calls=None):
        self.response = deepcopy(response)
        self.calls = calls if calls is not None else Calls()

    def find_related(self, request: RefreshRequest) -> RelatedDocuments:
        self.calls.record("relations.find_related", request)
        # Deliberately returns malformed/out-of-scope fixtures too: rules own validation.
        return deepcopy(self.response)


class FakeDocuments:
    def __init__(self, documents=(), calls=None):
        self.documents = {(d.project_id, d.document_id): deepcopy(d) for d in documents}
        self.calls = calls if calls is not None else Calls()

    def fetch_documents(self, project_id: str, document_ids: tuple[str, ...]) -> DocumentBatch:
        self.calls.record("documents.fetch_documents", project_id, document_ids)
        found, missing = [], []
        for document_id in document_ids:
            document = self.documents.get((project_id, document_id))
            if document is None:
                missing.append(document_id)
            else:
                found.append(deepcopy(document))
        return DocumentBatch(tuple(found), tuple(missing))


class FakeModel:
    def __init__(self, candidate, calls=None):
        self.candidate = deepcopy(candidate)
        self.calls = calls if calls is not None else Calls()

    def generate(self, model_input):
        self.calls.record("model.generate", model_input)
        return deepcopy(self.candidate)
