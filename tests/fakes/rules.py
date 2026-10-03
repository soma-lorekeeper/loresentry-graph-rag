"""Scripted internal steps for wiring tests, NOT implemented business policies."""

from app.refresh.models import Chunk, ModelInput
from app.refresh.rules import RefreshRules
from tests.fakes.refresh import Calls


class ScriptedRules(RefreshRules):
    def __init__(self, selection, snapshot, result, calls=None):
        self.selection = selection
        self.snapshot = snapshot
        self.result = result
        self.calls = calls if calls is not None else Calls()
        self.model_input = ModelInput(
            "fixture prompt", "fixture-v1", "fixture-v1", "fake"
        )

    def validate_input(self, request, source):
        self.calls.record("rules.validate_input", request, source)

    def select(self, request, source, related):
        self.calls.record("rules.select", request, source, related)
        return self.selection

    def assemble(self, request, source, related, selection, fetched):
        self.calls.record(
            "rules.assemble", request, source, related, selection, fetched
        )
        return self.snapshot

    def chunk(self, snapshot):
        self.calls.record("rules.chunk", snapshot)
        return tuple(
            Chunk(e) for p in self.result.document_proposals for e in p.evidence
        )

    def build_context(self, snapshot, chunks):
        self.calls.record("rules.build_context", snapshot, chunks)
        return "fixture context"

    def build_prompt(self, context):
        self.calls.record("rules.build_prompt", context)
        return self.model_input

    def validate_candidate(self, snapshot, candidate, model_input):
        self.calls.record("rules.validate_candidate", snapshot, candidate, model_input)
        return self.result
