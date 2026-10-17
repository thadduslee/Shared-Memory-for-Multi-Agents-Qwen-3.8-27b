"""FastAPI application.

Agent routes (``/v1``) require a trusted principal context from the configured
authenticator. Administrative routes (``/admin``) are separate, require the
admin key, and are disabled when no key is configured. Error bodies carry only
a reason code and a fixed message; request validation errors never echo input.
"""

# NOTE: no ``from __future__ import annotations`` here. Route signatures use a
# dependency alias defined inside create_app; string annotations would make
# FastAPI treat the principal context as a request parameter instead of a
# mandatory authentication dependency.
import hmac
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from gsm.auth import DevHmacAuthenticator
from gsm.config import Settings
from gsm.db.database import Database
from gsm.domain.context import PrincipalContext
from gsm.domain.operations import (
    AddRelationship,
    CommitReceipt,
    DeletionReceipt,
    DeletionRequest,
    DeriveRequest,
    GrantAccess,
    NaturalLanguageEvent,
    OperationBatch,
    RemoveRelationship,
    RestrictAccess,
    RetireFact,
    RevokeAccess,
    StructuredEvent,
    SupersedeFact,
    UpdateFact,
)
from gsm.domain.reasons import Reason
from gsm.domain.types import View
from gsm.errors import ConfigurationError, ContextError, ForbiddenError, GsmError, NotFoundError
from gsm.providers.factory import build_providers
from gsm.service import (
    DeriveResult,
    GovernedMemoryService,
    IngestResult,
    MemoryView,
    PublicEvidence,
    QueryRequest,
    QueryResponse,
)


class _Admin(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TenantIn(_Admin):
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    display_name: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class PrincipalIn(_Admin):
    principal_id: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    display_name: str = Field(min_length=1, max_length=200)
    kind: Literal["human", "agent", "service"] = "human"
    roles: list[str] = Field(default_factory=list)


class RoleIn(_Admin):
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    valid_until: datetime | None = None


class RelationshipIn(_Admin):
    subject_principal_id: str
    relation: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    object_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    object_id: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    valid_until: datetime | None = None


class DenialIn(_Admin):
    principal_id: str
    resource_type: Literal["memory", "compartment", "subject"]
    resource_id: str
    valid_until: datetime | None = None


def create_app(
    settings: Settings | None = None,
    service: GovernedMemoryService | None = None,
    authenticator: Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_for_startup()
    if service is None:
        assert settings.database_url is not None
        db = Database(settings.database_url, app_role=settings.db_app_role)
        service = GovernedMemoryService(db, settings, providers=build_providers(settings))
    if authenticator is None:
        if settings.dev_auth_enabled:
            if settings.env == "production":
                raise ConfigurationError(Reason.UNSUPPORTED_OPERATION, "dev auth disabled in production")
            assert settings.dev_auth_secret is not None
            authenticator = DevHmacAuthenticator(service, settings.dev_auth_secret.get_secret_value().encode())
        else:
            authenticator = None  # no request can authenticate: fail closed

    app = FastAPI(
        title="Governed Shared Memory",
        version="0.1.0",
        description="Policy-enforced, temporal, provenance-aware shared memory for multiple agents (research MVP).",
    )
    app.state.service = service
    app.state.settings = settings

    @app.exception_handler(GsmError)
    async def _gsm_error(_request: Request, exc: GsmError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.reason.value, "message": exc.safe_message}
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Locations and error types only: never echo submitted values.
        errors = [{"loc": [str(p) for p in e.get("loc", ())], "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": Reason.INVALID_OPERATION.value, "details": errors})

    def principal(request: Request) -> PrincipalContext:
        if authenticator is None:
            raise ContextError(Reason.CONTEXT_MISSING)
        ctx: PrincipalContext = authenticator.authenticate({k.lower(): v for k, v in request.headers.items()})
        return ctx

    def admin(request: Request) -> None:
        key = settings.admin_api_key
        if key is None:
            raise NotFoundError()
        supplied = request.headers.get("x-gsm-admin-key", "")
        if not hmac.compare_digest(supplied.encode(), key.get_secret_value().encode()):
            raise ForbiddenError(Reason.CONTEXT_UNTRUSTED)

    Ctx = Annotated[PrincipalContext, Depends(principal)]
    svc = service

    # ---------------------------------------------------------------- health
    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def readyz() -> JSONResponse:
        ok = svc.db.ping()
        return JSONResponse(status_code=200 if ok else 503, content={"database": "ok" if ok else "unavailable"})

    # ----------------------------------------------------------------- agent
    v1 = APIRouter(prefix="/v1", tags=["agent"])

    @v1.post("/events", response_model=CommitReceipt)
    def submit_event(event: StructuredEvent, ctx: Ctx) -> CommitReceipt:
        return svc.submit_event(ctx, event)

    @v1.post("/events/interpret")
    def interpret_event(event: NaturalLanguageEvent, ctx: Ctx) -> dict[str, Any]:
        proposals = svc.interpret_event(ctx, event)
        return {"committed": False, "proposals": [p.model_dump(mode="json") for p in proposals]}

    @v1.post("/events/ingest", response_model=IngestResult)
    def ingest_event(event: NaturalLanguageEvent, ctx: Ctx) -> IngestResult:
        return svc.ingest_natural_language(ctx, event)

    @v1.post("/operations", response_model=CommitReceipt)
    def commit(batch: OperationBatch, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, batch.operations)

    @v1.post("/query", response_model=QueryResponse)
    def query(req: QueryRequest, ctx: Ctx) -> QueryResponse:
        return svc.query(ctx, req)

    @v1.post("/search", response_model=list[PublicEvidence])
    def search(req: QueryRequest, ctx: Ctx) -> list[PublicEvidence]:
        return list(svc.search(ctx, req))

    @v1.get("/memories/{memory_id}", response_model=MemoryView)
    def get_memory(memory_id: str, ctx: Ctx, as_of: datetime | None = None, max_view: View | None = None) -> MemoryView:
        return svc.get_memory(ctx, memory_id, as_of=as_of, max_view=max_view)

    @v1.get("/memories/{memory_id}/history", response_model=list[MemoryView])
    def get_history(memory_id: str, ctx: Ctx) -> list[MemoryView]:
        return svc.get_history(ctx, memory_id)

    @v1.post("/memories/update", response_model=CommitReceipt)
    def update(op: UpdateFact, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [op])

    @v1.post("/memories/supersede", response_model=CommitReceipt)
    def supersede(op: SupersedeFact, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [op])

    @v1.post("/memories/retire", response_model=CommitReceipt)
    def retire(op: RetireFact, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [op])

    @v1.post("/memories/restrict", response_model=CommitReceipt)
    def restrict(op: RestrictAccess, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [op])

    @v1.post("/memories/derive", response_model=DeriveResult)
    def derive(req: DeriveRequest, ctx: Ctx) -> DeriveResult:
        return svc.derive(ctx, req)

    @v1.post("/grants", response_model=CommitReceipt)
    def grant(op: GrantAccess, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [op])

    @v1.delete("/grants/{grant_id}", response_model=CommitReceipt)
    def revoke(grant_id: str, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [RevokeAccess(grant_id=grant_id)])

    @v1.post("/relationships", response_model=CommitReceipt)
    def add_relationship(op: AddRelationship, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [op])

    @v1.delete("/relationships/{relationship_id}", response_model=CommitReceipt)
    def remove_relationship(relationship_id: str, ctx: Ctx) -> CommitReceipt:
        return svc.commit_operations(ctx, [RemoveRelationship(relationship_id=relationship_id)])

    @v1.post("/forget", response_model=DeletionReceipt)
    def forget(req: DeletionRequest, ctx: Ctx) -> DeletionReceipt:
        return svc.forget(ctx, req)

    @v1.get("/receipts/{receipt_id}")
    def receipt(receipt_id: str, ctx: Ctx) -> dict[str, Any]:
        return svc.get_receipt(ctx, receipt_id)

    @v1.get("/cascades/{cascade_id}")
    def cascade(cascade_id: str, ctx: Ctx) -> dict[str, Any]:
        return asdict(svc.get_cascade_status(ctx, cascade_id))

    app.include_router(v1)

    # ----------------------------------------------------------------- admin
    adm = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin)])

    @adm.post("/tenants", status_code=201)
    def create_tenant(body: TenantIn) -> dict[str, str]:
        svc.create_tenant(body.tenant_id, body.display_name, body.workspace_id)
        return {"tenant_id": body.tenant_id, "workspace_id": body.workspace_id}

    @adm.delete("/tenants/{tenant_id}")
    def reset_tenant(tenant_id: str) -> dict[str, str]:
        svc.reset_tenant(tenant_id)
        return {"tenant_id": tenant_id, "status": "deleted"}

    @adm.post("/tenants/{tenant_id}/principals", status_code=201)
    def create_principal(tenant_id: str, body: PrincipalIn) -> dict[str, str]:
        svc.create_principal(tenant_id, body.principal_id, body.display_name, kind=body.kind, roles=body.roles)
        return {"principal_id": body.principal_id}

    @adm.post("/tenants/{tenant_id}/principals/{principal_id}/roles")
    def bind_role(tenant_id: str, principal_id: str, body: RoleIn) -> dict[str, str]:
        svc.bind_role(tenant_id, principal_id, body.role, valid_until=body.valid_until)
        return {"status": "bound"}

    @adm.delete("/tenants/{tenant_id}/principals/{principal_id}/roles/{role}")
    def revoke_role(tenant_id: str, principal_id: str, role: str) -> dict[str, str]:
        svc.revoke_role(tenant_id, principal_id, role)
        return {"status": "revoked"}

    @adm.post("/tenants/{tenant_id}/principals/{principal_id}/deactivate")
    def deactivate(tenant_id: str, principal_id: str) -> dict[str, str]:
        svc.set_principal_active(tenant_id, principal_id, False)
        return {"status": "deactivated"}

    @adm.post("/tenants/{tenant_id}/relationships", status_code=201)
    def admin_relationship(tenant_id: str, body: RelationshipIn) -> dict[str, str]:
        rid = svc.add_relationship_admin(
            tenant_id,
            body.subject_principal_id,
            body.relation,
            body.object_type,
            body.object_id,
            valid_until=body.valid_until,
        )
        return {"relationship_id": rid}

    @adm.delete("/tenants/{tenant_id}/relationships/{relationship_id}")
    def admin_remove_relationship(tenant_id: str, relationship_id: str) -> dict[str, str]:
        svc.remove_relationship_admin(tenant_id, relationship_id)
        return {"status": "removed"}

    @adm.post("/tenants/{tenant_id}/denials", status_code=201)
    def add_denial(tenant_id: str, body: DenialIn) -> dict[str, str]:
        did = svc.add_denial(
            tenant_id, body.principal_id, body.resource_type, body.resource_id, valid_until=body.valid_until
        )
        return {"denial_id": did}

    @adm.post("/tenants/{tenant_id}/cascades/run")
    def run_cascades(tenant_id: str) -> list[dict[str, Any]]:
        return [asdict(s) for s in svc.run_cascades(tenant_id)]

    @adm.post("/tenants/{tenant_id}/reindex")
    def reindex(tenant_id: str) -> dict[str, int]:
        return {"versions_reindexed": svc.reindex(tenant_id)}

    @adm.get("/tenants/{tenant_id}/audit")
    def audit(tenant_id: str, limit: int = 200) -> dict[str, Any]:
        return svc.audit_export(tenant_id, limit=min(max(limit, 1), 5000))

    @adm.get("/stats")
    def stats() -> dict[str, Any]:
        return {"service": dict(svc.stats), "cache": {"hits": svc.cache.hits, "misses": svc.cache.misses}}

    app.include_router(adm)
    return app
