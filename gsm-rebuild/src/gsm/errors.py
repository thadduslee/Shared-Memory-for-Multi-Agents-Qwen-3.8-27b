"""Content-free exceptions.

Every error carries a stable reason code and a fixed message. Messages never
interpolate memory content, query text or model output, so exceptions are
safe to log and to return to callers.
"""

from __future__ import annotations

from gsm.domain.reasons import Reason


class GsmError(Exception):
    status_code = 400
    default_message = "The request could not be processed."

    def __init__(self, reason: Reason, message: str | None = None) -> None:
        self.reason = reason
        self.safe_message = message or self.default_message
        super().__init__(f"{reason.value}: {self.safe_message}")


class ContextError(GsmError):
    """Trusted request context is missing, malformed or untrusted. Always fails closed."""

    status_code = 401
    default_message = "Authentication required."


class NotFoundError(GsmError):
    """Uniform not-found. Used for missing, cross-tenant and unauthorized targets alike."""

    status_code = 404
    default_message = "Not found."

    def __init__(self) -> None:
        super().__init__(Reason.NOT_FOUND)


class ForbiddenError(GsmError):
    status_code = 403
    default_message = "Not permitted."


class ConflictError(GsmError):
    status_code = 409
    default_message = "The request conflicts with the current state."


class ValidationFailure(GsmError):
    status_code = 422
    default_message = "The request is invalid."


class ConfigurationError(GsmError):
    status_code = 500
    default_message = "Service misconfigured."


class ProviderError(Exception):
    """A model/embedding provider failed. Carries only a category, never payloads."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"provider failure: {category}")
