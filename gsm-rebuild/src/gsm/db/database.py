"""Database access with a structural tenant boundary.

Every statement issued through ``Tx`` must reference ``:tenant_id``; the value
is injected from the transaction, never from the caller. Each tenant
transaction also sets the ``gsm.tenant_id`` setting that the forced row-level
security policies check, and connections switch to the non-superuser
``gsm_app`` role so those policies actually apply. Either layer alone would
stop a cross-tenant read; both are kept on purpose.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class TenantScopeViolation(RuntimeError):
    """Raised when code tries to run SQL without a tenant predicate."""


class Tx:
    """A tenant-bound transaction."""

    def __init__(self, conn: Connection, tenant_id: str) -> None:
        self.conn = conn
        self.tenant_id = tenant_id

    def _params(self, sql: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if ":tenant_id" not in sql:
            raise TenantScopeViolation("statement is not tenant-scoped")
        if "tenant_id" in params:
            raise TenantScopeViolation("tenant_id is injected by the transaction")
        return {**params, "tenant_id": self.tenant_id}

    def all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        rows = self.conn.execute(text(sql), self._params(sql, params)).mappings().all()
        return [dict(r) for r in rows]

    def one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        row = self.conn.execute(text(sql), self._params(sql, params)).mappings().first()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, **params: Any) -> Any:
        return self.conn.execute(text(sql), self._params(sql, params)).scalar()

    def exec(self, sql: str, **params: Any) -> int:
        result = self.conn.execute(text(sql), self._params(sql, params))
        return int(result.rowcount or 0)

    def savepoint(self) -> Any:
        return self.conn.begin_nested()


def jsonb(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


class Database:
    def __init__(self, url: str, *, app_role: str | None = "gsm_app", pool_size: int = 10) -> None:
        if app_role is not None and not _ROLE_RE.match(app_role):
            raise ValueError("invalid application role name")
        self.app_role = app_role
        self.engine: Engine = create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=pool_size)
        if app_role is not None:
            role = app_role

            @event.listens_for(self.engine, "connect")
            def _set_role(dbapi_conn: Any, _record: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute(f'SET ROLE "{role}"')
                cur.close()
                dbapi_conn.commit()

    @contextmanager
    def tenant_tx(self, tenant_id: str) -> Iterator[Tx]:
        with self.engine.begin() as conn:
            conn.execute(text("SELECT set_config('gsm.tenant_id', :t, true)"), {"t": tenant_id})
            yield Tx(conn, tenant_id)

    @contextmanager
    def system_tx(self) -> Iterator[Connection]:
        """Tenant-less transaction. RLS hides every tenant-owned row here;
        only the ``tenants`` catalog is visible."""
        with self.engine.begin() as conn:
            yield conn

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except DBAPIError:
            return False

    def dispose(self) -> None:
        self.engine.dispose()


def is_unique_violation(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "23505"


def is_check_violation(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "23514"
