"""Expected port/policy failures are separate from implementation defects."""

from app.refresh.models import Failure


class RefreshFailure(Exception):
    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.message)


class RequestConflict(Exception):
    """A request ID was reused with different inputs; never overwrite its result."""


class JobBusy(Exception):
    """Another execution owns the job; delivery may retry later."""


class RulesNotImplemented(NotImplementedError):
    """A development boundary, not a normal business failure or success."""
