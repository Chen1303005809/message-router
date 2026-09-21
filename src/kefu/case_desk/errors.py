"""Domain errors deliberately independent of FastAPI and WeCom."""

from __future__ import annotations


class CaseDeskError(Exception):
    """Base class for errors callers can render as a safe user-facing outcome."""


class NotFound(CaseDeskError):
    pass


class Forbidden(CaseDeskError):
    pass


class ValidationError(CaseDeskError):
    pass


class Conflict(CaseDeskError):
    """A concurrent change means the caller must reload before retrying."""


class RoutingUnavailable(CaseDeskError):
    """A user/team/channel mapping is missing, inactive, or ambiguous."""


class InvariantViolation(CaseDeskError):
    pass
