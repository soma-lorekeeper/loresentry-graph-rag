from dataclasses import dataclass

from app.refresh.models import Selection
from app.refresh.service import RefreshService
from tests.fakes.fixtures import example
from tests.fakes.refresh import Calls, FakeArtifacts, FakeDocuments, FakeJobs, FakeModel, FakePublisher, FakeRelations
from tests.fakes.rules import ScriptedRules


@dataclass
class Scenario:
    request: object
    source: object
    snapshot: object
    result: object
    calls: Calls
    artifacts: FakeArtifacts
    relations: FakeRelations
    documents: FakeDocuments
    model: FakeModel
    jobs: FakeJobs
    publisher: FakePublisher
    rules: ScriptedRules

    def service(self, *, use_stub=True):
        return RefreshService(artifacts=self.artifacts, relations=self.relations,
                              documents=self.documents, model=self.model, jobs=self.jobs,
                              publisher=self.publisher, rules=self.rules if use_stub else None)


def scenario():
    request, source, related, snapshot, candidate, result = example()
    calls = Calls()
    return Scenario(request, source, snapshot, result, calls,
                    FakeArtifacts({request.input_ref: source}, calls),
                    FakeRelations(related, calls), FakeDocuments(snapshot.documents[1:], calls),
                    FakeModel(candidate, calls), FakeJobs(calls=calls), FakePublisher(calls),
                    ScriptedRules(Selection(("draft-1", "setting-1"), ("setting-1",)), snapshot, result, calls))
