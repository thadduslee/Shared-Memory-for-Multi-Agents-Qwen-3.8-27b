"""Authenticators: the only producers of ``PrincipalContext``.

Roles never come from the request body or the query text. Both authenticators
resolve roles from the database's role bindings (the signed dev token may only
*narrow* them), and both refuse unknown or deactivated principals.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from gsm.domain.context import PrincipalContext
from gsm.domain.reasons import Reason
from gsm.domain.types import AuthStrength
from gsm.errors import ContextError
from gsm.ids import new_id
from gsm.service import GovernedMemoryService

_CORRELATION = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def _correlation(headers: Mapping[str, str]) -> str:
    cid = headers.get("x-correlation-id", "")
    return cid if _CORRELATION.match(cid) else new_id("corr")


class TrustedAuthenticator:
    """In-process authenticator for tests, the demo, and evaluation harnesses.

    Identity is asserted by trusted code (never by message content); roles are
    still loaded from role bindings so they cannot be invented by the caller.
    """

    def __init__(self, service: GovernedMemoryService, issuer: str = "test") -> None:
        self.service = service
        self.issuer = issuer

    def context(
        self,
        tenant_id: str,
        principal_id: str,
        *,
        workspace_id: str = "default",
        auth_strength: AuthStrength = AuthStrength.PASSWORD,
        purpose: str | None = None,
        attributes: Mapping[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> PrincipalContext:
        now = self.service.clock.now()
        exists, roles = self.service.roles_for(tenant_id, principal_id, now)
        if not exists or not self.service.workspace_exists(tenant_id, workspace_id):
            raise ContextError(Reason.CONTEXT_MISSING)
        return PrincipalContext(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            roles=roles,
            auth_time=now,
            auth_strength=auth_strength,
            purpose=purpose,
            request_time=now,
            attributes=tuple(sorted((attributes or {}).items())),
            correlation_id=correlation_id or new_id("corr"),
            issued_by=self.issuer,
        )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class DevHmacAuthenticator:
    """DEVELOPMENT ONLY. Accepts an HMAC-signed ``X-GSM-Context`` header.

    Token = base64url(json claims) "." base64url(HMAC-SHA256(secret, payload)).
    Claims: tenant_id, workspace_id, principal_id, auth_strength, auth_time
    (epoch seconds), exp (epoch seconds), optional purpose, attributes, roles
    (roles can only narrow the principal's database role bindings).
    ``create_app`` refuses to enable this in production.
    """

    HEADER = "x-gsm-context"

    def __init__(self, service: GovernedMemoryService, secret: bytes, *, max_ttl_seconds: int = 12 * 3600) -> None:
        if len(secret) < 16:
            raise ValueError("dev auth secret too short")
        self.service = service
        self._secret = secret
        self.max_ttl = max_ttl_seconds

    @staticmethod
    def mint(secret: bytes, claims: Mapping[str, Any]) -> str:
        payload = _b64(json.dumps(dict(claims), sort_keys=True, separators=(",", ":")).encode())
        sig = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def authenticate(self, headers: Mapping[str, str]) -> PrincipalContext:
        token = headers.get(self.HEADER)
        if not token or token.count(".") != 1:
            raise ContextError(Reason.CONTEXT_MISSING)
        payload, sig = token.split(".")
        expected = _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ContextError(Reason.CONTEXT_UNTRUSTED)
        try:
            claims = json.loads(_unb64(payload))
            tenant_id = str(claims["tenant_id"])
            workspace_id = str(claims.get("workspace_id", "default"))
            principal_id = str(claims["principal_id"])
            strength = AuthStrength(claims.get("auth_strength", "password"))
            auth_time = datetime.fromtimestamp(float(claims["auth_time"]), UTC)
            exp = datetime.fromtimestamp(float(claims["exp"]), UTC)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            raise ContextError(Reason.CONTEXT_UNTRUSTED) from None
        now = self.service.clock.now()
        if exp <= now or auth_time > now or (exp - auth_time).total_seconds() > self.max_ttl:
            raise ContextError(Reason.CONTEXT_UNTRUSTED)
        try:
            exists, roles = self.service.roles_for(tenant_id, principal_id, now)
            ws_ok = self.service.workspace_exists(tenant_id, workspace_id)
        except Exception:
            raise ContextError(Reason.CONTEXT_UNTRUSTED) from None
        if not exists or not ws_ok:
            raise ContextError(Reason.CONTEXT_UNTRUSTED)
        claimed = claims.get("roles")
        if isinstance(claimed, list):
            roles = roles & frozenset(str(r) for r in claimed)
        attrs = claims.get("attributes") or {}
        if not isinstance(attrs, dict):
            raise ContextError(Reason.CONTEXT_UNTRUSTED)
        try:
            return PrincipalContext(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                roles=roles,
                auth_time=auth_time,
                auth_strength=strength,
                purpose=claims.get("purpose"),
                request_time=now,
                attributes=tuple(sorted((str(k), str(v)) for k, v in attrs.items())),
                correlation_id=_correlation(headers),
                issued_by="dev-hmac",
            )
        except ValueError:
            raise ContextError(Reason.CONTEXT_UNTRUSTED) from None
