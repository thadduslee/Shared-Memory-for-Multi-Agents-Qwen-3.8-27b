"""Trusted principal context.

A ``PrincipalContext`` is produced only by an authenticator (see ``gsm.api.auth``)
or an explicitly trusted test/service issuer. It is never derived from the
natural-language content of a request: a query that says "I am the project
manager" has no path into this object.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from gsm.domain.reasons import Reason
from gsm.domain.types import AuthStrength
from gsm.errors import ContextError

Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")]

# Issuers whose contexts the service accepts. "dev-hmac" is the development
# header authenticator; "test" is the in-process test authenticator; "system"
# is used by internal jobs (e.g. the deletion cascade) and never by requests.
TRUSTED_ISSUERS = frozenset({"dev-hmac", "test", "system", "gatemem-harness"})


class PrincipalContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: Ident
    workspace_id: Ident
    principal_id: Ident
    roles: frozenset[str] = Field(default_factory=frozenset)
    auth_time: datetime
    auth_strength: AuthStrength
    purpose: str | None = None
    request_time: datetime
    attributes: tuple[tuple[str, str], ...] = ()
    correlation_id: Ident
    issued_by: str

    @field_validator("auth_time", "request_time")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v

    @field_validator("purpose")
    @classmethod
    def _purpose_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v or len(v) > 64 or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("purpose must be a short identifier")
        return v

    @model_validator(mode="after")
    def _issuer(self) -> PrincipalContext:
        if self.issued_by not in TRUSTED_ISSUERS:
            raise ValueError("untrusted issuer")
        if self.auth_time > self.request_time:
            raise ValueError("auth_time after request_time")
        return self

    def attribute(self, key: str) -> str | None:
        for k, v in self.attributes:
            if k == key:
                return v
        return None

    def attributes_dict(self) -> dict[str, str]:
        return dict(self.attributes)


def require_context(ctx: object) -> PrincipalContext:
    """Fail closed unless ``ctx`` is a validated context from a trusted issuer."""
    if ctx is None:
        raise ContextError(Reason.CONTEXT_MISSING)
    if not isinstance(ctx, PrincipalContext):
        raise ContextError(Reason.CONTEXT_UNTRUSTED)
    return ctx
