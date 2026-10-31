"""Errors which callers may report without an internal traceback."""


class ResearchError(Exception):
    """A rejected operation or unavailable research record."""


class ValidationError(ResearchError, ValueError):
    pass


class NotFoundError(ResearchError, LookupError):
    pass


class ConflictError(ResearchError):
    pass


class BudgetError(ConflictError):
    pass


class CheckpointBaseRequired(ValidationError):
    code = "checkpoint_base_required"
    details = {"read": {"collection": "checkpoint"}}


class CheckpointBaseMismatch(ValidationError):
    code = "checkpoint_base_mismatch"
    details = {"read": {"collection": "checkpoint"}}


class CheckpointConflict(ConflictError):
    code = "checkpoint_conflict"

    def __init__(self, message, *, current_revision):
        super().__init__(message)
        self.details = {"current_revision": current_revision, "read": {"collection": "checkpoint"}}
