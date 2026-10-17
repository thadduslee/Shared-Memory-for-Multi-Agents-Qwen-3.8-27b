"""GovernedMemoryService: the only way agents read or write shared memory.

Agents and model adapters never touch the database. Every call takes a
trusted ``PrincipalContext`` and runs the mandatory pipeline:

    query:  context -> plan -> authorized candidates (SQL superset + engine)
            -> ranking inside the authorized set -> temporal resolution
            -> permitted projections -> disclosure plan -> generation
            -> live re-check + deterministic release verification
            -> (optional, stricter-only) semantic verification -> audit

    write:  context -> validate -> triage (quarantine) -> authorize each op
            -> apply all ops in one transaction -> receipts + decisions

    forget: context -> exact targets -> authorize -> tombstone + quarantine
            descendants in one transaction -> receipt -> cascade (inline or
            deferred) -> cache eviction
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from gsm import POLICY_ENGINE_VERSION
from gsm.audit.logging import get_logger
from gsm.audit.recorder import AuditRecorder
from gsm.cache import QueryCache
from gsm.clock import Clock, SystemClock
from gsm.config import Settings
from gsm.db.database import Database, Tx, jsonb
from gsm.disclosure.planner import DisclosurePlan, plan_disclosure
from gsm.disclosure.schemas import get_schema, hidden_values
from gsm.domain.context import PrincipalContext, require_context
from gsm.domain.operations import (
    CandidateMemoryOperation,
    CommitReceipt,
    DeletionReceipt,
    DeletionRequest,
    DeriveRequest,
    NaturalLanguageEvent,
    NoMemoryChange,
    OperationResult,
    StructuredEvent,
)
from gsm.domain.reasons import Reason
from gsm.domain.types import AuthStrength, OperationStatus, Origin, QueryAction, View, cap_view
from gsm.errors import ConflictError, ContextError, ForbiddenError, NotFoundError, ValidationFailure
from gsm.ids import new_id
from gsm.memory.cascade import CascadeRunner, CascadeStatus, cascade_status
from gsm.memory.errors import OpRejected
from gsm.memory.forget import tombstone_in_tx
from gsm.memory.provenance import derive_in_tx
from gsm.memory.repo import (
    ancestors,
    build_index_rows,
    facts_from_row,
    insert_index_rows,
    load_atom_meta,
    load_content,
    load_snapshot,
    resolve_roles,
)
from gsm.memory.writes import WriteExecutor, insert_relationship
from gsm.policy.decision import PolicyDecision
from gsm.policy.engine import PolicyEngine
from gsm.policy.facts import AuthSnapshot
from gsm.policy.lineage import resolve
from gsm.providers.base import (
    AnswerGenerator,
    DraftResponse,
    Embedder,
    EventInterpreter,
    GeneratorEvidence,
    InterpreterInput,
    SemanticVerifier,
)
from gsm.providers.fake import HashingEmbedder, RuleBasedInterpreter
from gsm.release.generator import CANONICAL_TEXT, DeterministicGenerator
from gsm.release.verifier import ProtectedValues, ReleaseDecision, ReleaseMetadata, ReleaseVerifier
from gsm.retrieval.candidates import CandidateSet, list_authorized_candidates
from gsm.retrieval.evidence import EvidenceBundle, EvidenceItem
from gsm.retrieval.planner import DeterministicPlanner, QueryPlan, QueryPlanner
from gsm.retrieval.search import RankingConfig, all_within, rank_within

log = get_logger("service")
ANSWER_ACTIONS = (QueryAction.ANSWER, QueryAction.ANSWER_REDACTED)


# ----------------------------------------------------------------- API models
class QueryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=100)
    as_of: datetime | None = None
    # Can only lower the permitted view, never raise it.
    max_view: View | None = None


class PublicEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_id: str | None
    version_id: str | None
    view: View
    semantic_type: str
    text: str
    fields: dict[str, str]
    observed_at: datetime | None
    valid_from: datetime | None
    valid_until: datetime | None
    truth_state: str | None
    is_derived: bool


class QueryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    correlation_id: str
    release_id: str
    action: QueryAction
    answer: str
    evidence: tuple[PublicEvidence, ...]
    reason_codes: tuple[str, ...]
    policy_version: str
    cache_hit: bool = False


class MemoryView(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_id: str
    version_id: str
    view: View
    semantic_type: str
    topic: str
    text: str
    fields: dict[str, str]
    observed_at: datetime
    valid_from: datetime
    valid_until: datetime | None
    version_state: str
    truth_state: str
    is_derived: bool


class DeriveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    receipt_id: str
    memory_id: str
    version_id: str
    reason_codes: tuple[str, ...]
    inherits_parent_policy: bool


class IngestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    interpreter: str
    proposals: tuple[CandidateMemoryOperation, ...]
    receipt: CommitReceipt | None


@dataclass
class PipelineHooks:
    """Test seams for demonstrating race safety. Never used in production paths."""

    after_candidates: Callable[[PrincipalContext, CandidateSet], None] | None = None
    before_generate: Callable[[Sequence[GeneratorEvidence]], None] | None = None
    before_release: Callable[[PrincipalContext], None] | None = None
    # Diagnostics: every ranking candidate with coverage, similarity and fate.
    on_ranking: Callable[[list[dict[str, Any]]], None] | None = None


@dataclass
class Providers:
    embedder: Embedder | None = field(default_factory=HashingEmbedder)
    interpreter: EventInterpreter = field(default_factory=RuleBasedInterpreter)
    generator: AnswerGenerator = field(default_factory=DeterministicGenerator)
    semantic_verifier: SemanticVerifier | None = None
    planner: QueryPlanner = field(default_factory=DeterministicPlanner)


@dataclass
class _Cached:
    response: QueryResponse
    plan: DisclosurePlan
    as_of: datetime | None


def _public(item: EvidenceItem) -> PublicEvidence:
    """Caller-facing evidence. Aggregate items carry no id or timestamps."""
    agg = item.view is View.AGGREGATE
    return PublicEvidence(
        memory_id=None if agg else item.memory_id,
        version_id=None if agg else item.version_id,
        view=item.view,
        semantic_type=item.semantic_type,
        text=item.text,
        fields=dict(item.fields),
        observed_at=None if agg else item.observed_at,
        valid_from=None if agg else item.valid_from,
        valid_until=None if agg else item.valid_until,
        truth_state=item.truth_state,
        is_derived=item.is_derived,
    )


class _Rollback(Exception):
    def __init__(self, index: int, reasons: tuple[Reason, ...]) -> None:
        self.index = index
        self.reasons = reasons


def _constraint(exc: BaseException) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return str(getattr(diag, "constraint_name", "") or "")


class GovernedMemoryService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        clock: Clock | None = None,
        providers: Providers | None = None,
        engine: PolicyEngine | None = None,
        audit: AuditRecorder | None = None,
        hooks: PipelineHooks | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock or SystemClock()
        self.providers = providers or Providers()
        self.engine = engine or PolicyEngine()
        key = settings.audit_hmac_key.get_secret_value().encode() if settings.audit_hmac_key else None
        self.audit = audit or AuditRecorder(key)
        self.hooks = hooks or PipelineHooks()
        self.verifier = ReleaseVerifier()
        self.fallback_generator = DeterministicGenerator()
        self.cache: QueryCache[_Cached] = QueryCache(ttl_seconds=settings.cache_ttl_seconds)
        self.cascades = CascadeRunner(db, self.clock, self.cache.invalidate_tenant)
        self.ranking = RankingConfig(
            top_k=settings.top_k,
            lexical_weight=settings.lexical_weight,
            vector_weight=settings.vector_weight,
            vector_min_similarity=settings.vector_min_similarity,
            vector_support_similarity=settings.vector_support_similarity,
            min_coverage=settings.relevance_min_coverage,
            relative_cutoff=settings.relevance_relative_cutoff,
            dedup=settings.relevance_dedup,
        )
        self.stats: dict[str, int] = {
            "queries": 0,
            "cache_hits": 0,
            "release_blocks": 0,
            "fallback_renders": 0,
            "semantic_blocks": 0,
            "generator_failures": 0,
        }

    # ================================================================= admin
    def create_tenant(self, tenant_id: str, display_name: str, workspace_id: str = "default") -> None:
        now = self.clock.now()
        with self.db.system_tx() as conn:
            conn.execute(
                text("INSERT INTO tenants (tenant_id, display_name, created_at) VALUES (:t, :n, :now)"),
                {"t": tenant_id, "n": display_name, "now": now},
            )
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """INSERT INTO workspaces (tenant_id, workspace_id, display_name, created_at)
                   VALUES (:tenant_id, :ws, :ws, :now)""",
                ws=workspace_id,
                now=now,
            )

    def reset_tenant(self, tenant_id: str) -> None:
        """Delete a whole test tenant. Refused in production."""
        if self.settings.env == "production":
            raise ForbiddenError(Reason.UNSUPPORTED_OPERATION)
        with self.db.system_tx() as conn:
            conn.execute(text("DELETE FROM tenants WHERE tenant_id = :t"), {"t": tenant_id})
        self.cache.invalidate_tenant(tenant_id)

    def create_principal(
        self,
        tenant_id: str,
        principal_id: str,
        display_name: str,
        *,
        kind: Literal["human", "agent", "service"] = "human",
        roles: Sequence[str] = (),
        attributes: dict[str, str] | None = None,
    ) -> None:
        now = self.clock.now()
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """INSERT INTO principals (tenant_id, principal_id, display_name, kind, attributes, created_at)
                   VALUES (:tenant_id, :pid, :name, :kind, CAST(:attrs AS jsonb), :now)""",
                pid=principal_id,
                name=display_name,
                kind=kind,
                attrs=jsonb(attributes or {}),
                now=now,
            )
            for role in roles:
                self._bind_role(tx, principal_id, role, now)

    def _bind_role(self, tx: Tx, principal_id: str, role: str, now: datetime, until: datetime | None = None) -> None:
        tx.exec(
            """INSERT INTO roles (tenant_id, role_name, created_at) VALUES (:tenant_id, :r, :now)
               ON CONFLICT DO NOTHING""",
            r=role,
            now=now,
        )
        tx.exec(
            """INSERT INTO principal_roles (tenant_id, principal_id, role_name, valid_from, valid_until, granted_by,
                   created_at)
               VALUES (:tenant_id, :pid, :r, :now, :until, 'admin', :now)
               ON CONFLICT (tenant_id, principal_id, role_name)
               DO UPDATE SET revoked_at = NULL, valid_from = EXCLUDED.valid_from, valid_until = EXCLUDED.valid_until""",
            pid=principal_id,
            r=role,
            now=now,
            until=until,
        )

    def bind_role(self, tenant_id: str, principal_id: str, role: str, *, valid_until: datetime | None = None) -> None:
        with self.db.tenant_tx(tenant_id) as tx:
            self._bind_role(tx, principal_id, role, self.clock.now(), valid_until)
        self.cache.invalidate_tenant(tenant_id)

    def revoke_role(self, tenant_id: str, principal_id: str, role: str) -> None:
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """UPDATE principal_roles SET revoked_at = :now
                   WHERE tenant_id = :tenant_id AND principal_id = :pid AND role_name = :r AND revoked_at IS NULL""",
                pid=principal_id,
                r=role,
                now=self.clock.now(),
            )
        self.cache.invalidate_tenant(tenant_id)

    def set_principal_active(self, tenant_id: str, principal_id: str, active: bool) -> None:
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """UPDATE principals SET active = :a, deactivated_at = CASE WHEN :a THEN NULL ELSE :now END
                   WHERE tenant_id = :tenant_id AND principal_id = :pid""",
                a=active,
                now=self.clock.now(),
                pid=principal_id,
            )
        self.cache.invalidate_tenant(tenant_id)

    def add_relationship_admin(
        self,
        tenant_id: str,
        subject: str,
        relation: str,
        object_type: str,
        object_id: str,
        *,
        valid_until: datetime | None = None,
    ) -> str:
        rid = new_id("rel")
        with self.db.tenant_tx(tenant_id) as tx:
            insert_relationship(
                tx, rid, subject, relation, object_type, object_id, self.clock.now(), valid_until, "admin"
            )
        self.cache.invalidate_tenant(tenant_id)
        return rid

    def remove_relationship_admin(self, tenant_id: str, relationship_id: str) -> None:
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """UPDATE relationships SET removed_at = :now
                   WHERE tenant_id = :tenant_id AND relationship_id = :rid AND removed_at IS NULL""",
                now=self.clock.now(),
                rid=relationship_id,
            )
        self.cache.invalidate_tenant(tenant_id)

    def add_denial(
        self,
        tenant_id: str,
        principal_id: str,
        resource_type: Literal["memory", "compartment", "subject"],
        resource_id: str,
        *,
        valid_until: datetime | None = None,
    ) -> str:
        did = new_id("deny")
        now = self.clock.now()
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """INSERT INTO explicit_denials (tenant_id, denial_id, resource_type, resource_id, principal_id,
                       reason_code, valid_from, valid_until, created_by, created_at)
                   VALUES (:tenant_id, :id, :rt, :rid, :pid, 'EXPLICIT_DENIAL', :now, :until, 'admin', :now)""",
                id=did,
                rt=resource_type,
                rid=resource_id,
                pid=principal_id,
                now=now,
                until=valid_until,
            )
        self.cache.invalidate_tenant(tenant_id)
        return did

    def add_compartment_grant(
        self, tenant_id: str, principal_id: str, compartment: str, *, expires_at: datetime | None = None
    ) -> str:
        gid = new_id("grant")
        now = self.clock.now()
        with self.db.tenant_tx(tenant_id) as tx:
            tx.exec(
                """INSERT INTO explicit_grants (tenant_id, grant_id, resource_type, compartment, grantee_principal_id,
                       valid_from, expires_at, granted_by, created_at)
                   VALUES (:tenant_id, :gid, 'compartment', :c, :pid, :now, :exp, 'admin', :now)""",
                gid=gid,
                c=compartment,
                pid=principal_id,
                now=now,
                exp=expires_at,
            )
        self.cache.invalidate_tenant(tenant_id)
        return gid

    def roles_for(self, tenant_id: str, principal_id: str, at: datetime) -> tuple[bool, frozenset[str]]:
        """(exists_and_active, roles) straight from the database, for authenticators."""
        with self.db.tenant_tx(tenant_id) as tx:
            row = tx.one(
                "SELECT active FROM principals WHERE tenant_id = :tenant_id AND principal_id = :pid",
                pid=principal_id,
            )
            if row is None or not row["active"]:
                return False, frozenset()
            return True, resolve_roles(tx, principal_id, at)

    def workspace_exists(self, tenant_id: str, workspace_id: str) -> bool:
        with self.db.tenant_tx(tenant_id) as tx:
            return (
                tx.one(
                    "SELECT 1 AS x FROM workspaces WHERE tenant_id = :tenant_id AND workspace_id = :ws",
                    ws=workspace_id,
                )
                is not None
            )

    # ================================================================ writes
    def _snapshot(self, tx: Tx, ctx: PrincipalContext) -> AuthSnapshot:
        snap = load_snapshot(tx, ctx)
        if not snap.principal_active:
            raise ContextError(Reason.PRINCIPAL_INACTIVE)
        return snap

    def commit_operations(
        self,
        ctx: PrincipalContext,
        operations: Sequence[CandidateMemoryOperation],
        *,
        origin: Origin = Origin.ORACLE,
        event_id: str | None = None,
    ) -> CommitReceipt:
        ctx = require_context(ctx)
        if not operations:
            raise ValidationFailure(Reason.INVALID_OPERATION)
        receipt_id = new_id("rcpt")
        results: list[OperationResult] = []
        executor: WriteExecutor | None = None
        rollback: _Rollback | None = None
        try:
            with self.db.tenant_tx(ctx.tenant_id) as tx:
                snap = self._snapshot(tx, ctx)
                executor = WriteExecutor(tx, ctx, snap, self.engine, self.providers.embedder, origin, event_id)
                for i, op in enumerate(operations):
                    try:
                        results.append(executor.apply(i, op))
                    except OpRejected as rej:
                        raise _Rollback(i, rej.reasons) from None
                    except IntegrityError as exc:
                        name = _constraint(exc)
                        reason = (
                            Reason.SEMANTIC_IDENTITY_CONFLICT
                            if "semantic_key" in name
                            else Reason.VERSION_CONFLICT
                            if "one_active" in name
                            else Reason.INVALID_OPERATION
                        )
                        raise _Rollback(i, (reason,)) from None
                    except DBAPIError:
                        raise _Rollback(i, (Reason.INVALID_OPERATION,)) from None
                status = self._batch_status(results)
                self._write_receipt(tx, ctx, receipt_id, status, results)
                self.audit.record_decisions(tx, ctx, f"write:{origin.value}", executor.decisions)
        except _Rollback as rb:
            rollback = rb
        except IntegrityError as exc:
            # Deferred constraint failures surface at commit time.
            reason = (
                Reason.SEMANTIC_IDENTITY_CONFLICT if "semantic_key" in _constraint(exc) else Reason.VERSION_CONFLICT
            )
            rollback = _Rollback(max(len(results) - 1, 0), (reason,))
        if rollback is not None:
            rolled: list[OperationResult] = []
            for i, op in enumerate(operations):
                if i == rollback.index:
                    rolled.append(
                        OperationResult(
                            index=i,
                            op=op.op,
                            status=OperationStatus.REJECTED,
                            reasons=tuple(r.value for r in rollback.reasons),
                        )
                    )
                else:
                    rolled.append(
                        OperationResult(
                            index=i,
                            op=op.op,
                            status=OperationStatus.ROLLED_BACK,
                            reasons=(Reason.BATCH_ROLLED_BACK.value,),
                        )
                    )
            status = "rejected" if len(operations) == 1 else "rolled_back"
            with self.db.tenant_tx(ctx.tenant_id) as tx:
                self._write_receipt(tx, ctx, receipt_id, status, rolled)
                if executor is not None:
                    self.audit.record_decisions(tx, ctx, f"write:{origin.value}", executor.decisions)
            return CommitReceipt(
                receipt_id=receipt_id,
                correlation_id=ctx.correlation_id,
                status=status,
                results=tuple(rolled),
                policy_version=POLICY_ENGINE_VERSION,
                event_id=event_id,
            )
        assert executor is not None
        if any(r.status is OperationStatus.COMMITTED for r in results):
            self.cache.invalidate_tenant(ctx.tenant_id)
        if executor.cascade_ids and self.settings.cascade_mode == "inline":
            self.cascades.run(ctx.tenant_id, executor.cascade_ids)
        return CommitReceipt(
            receipt_id=receipt_id,
            correlation_id=ctx.correlation_id,
            status=self._batch_status(results),
            results=tuple(results),
            policy_version=POLICY_ENGINE_VERSION,
            event_id=event_id,
        )

    @staticmethod
    def _batch_status(results: Sequence[OperationResult]) -> str:
        statuses = {r.status for r in results}
        if OperationStatus.QUARANTINED in statuses:
            return "partial_quarantine"
        if statuses <= {OperationStatus.NOOP}:
            return "noop"
        return "committed"

    def _write_receipt(
        self,
        tx: Tx,
        ctx: PrincipalContext,
        receipt_id: str,
        status: str,
        results: Sequence[OperationResult],
        *,
        memory_ids: Sequence[str] = (),
    ) -> None:
        reasons = sorted({r for res in results for r in res.reasons})
        memory_ids = sorted({res.memory_id for res in results if res.memory_id} | set(memory_ids))
        tx.exec(
            """INSERT INTO commit_receipts (tenant_id, receipt_id, correlation_id, principal_id, status,
                   operation_count, committed_count, rejected_count, quarantined_count, memory_ids, reason_codes,
                   policy_version, created_at)
               VALUES (:tenant_id, :rid, :cid, :pid, :status, :n, :c, :r, :q, CAST(:mids AS text[]),
                   CAST(:reasons AS text[]), :pv, :now)""",
            rid=receipt_id,
            cid=ctx.correlation_id,
            pid=ctx.principal_id,
            status=status,
            n=len(results),
            c=sum(1 for r in results if r.status is OperationStatus.COMMITTED),
            r=sum(1 for r in results if r.status is OperationStatus.REJECTED),
            q=sum(1 for r in results if r.status is OperationStatus.QUARANTINED),
            mids=memory_ids,
            reasons=reasons,
            pv=POLICY_ENGINE_VERSION,
            now=ctx.request_time,
        )

    def _store_event(
        self,
        tx: Tx,
        ctx: PrincipalContext,
        event_type: str,
        observed_at: datetime | None,
        payload: dict[str, Any] | None,
    ) -> str:
        event_id = new_id("evt")
        tx.exec(
            """INSERT INTO raw_events (tenant_id, workspace_id, event_id, source_principal_id, event_type, observed_at,
                   received_at, payload, payload_state)
               VALUES (:tenant_id, :ws, :eid, :pid, :etype, :obs, :now, CAST(:payload AS jsonb), :state)""",
            ws=ctx.workspace_id,
            eid=event_id,
            pid=ctx.principal_id,
            etype=event_type,
            obs=observed_at or ctx.request_time,
            now=ctx.request_time,
            payload=jsonb(payload) if payload is not None else None,
            state="present" if payload is not None else "none",
        )
        return event_id

    def submit_event(self, ctx: PrincipalContext, event: StructuredEvent) -> CommitReceipt:
        """Structured (oracle) ingestion: the caller supplies typed operations."""
        ctx = require_context(ctx)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            self._snapshot(tx, ctx)
            event_id = self._store_event(tx, ctx, event.event_type, event.observed_at, event.payload)
        ops = event.operations or (NoMemoryChange(),)
        receipt = self.commit_operations(ctx, ops, origin=Origin.ORACLE, event_id=event_id)
        self._drop_unreferenced_payload(ctx, event_id)
        return receipt

    def _drop_unreferenced_payload(self, ctx: PrincipalContext, event_id: str) -> None:
        """Raw payloads are kept only as provenance for committed memories.

        If nothing committed references the event (no-op, quarantine, rollback,
        or a deletion request), its payload is scrubbed at once, so text that
        no memory owns can never outlive a later deletion.
        """
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            tx.exec(
                """UPDATE raw_events SET payload = NULL, payload_state = 'scrubbed', payload_scrubbed_at = :now
                   WHERE tenant_id = :tenant_id AND event_id = :eid AND payload_state = 'present'
                     AND NOT EXISTS (SELECT 1 FROM memory_edges e
                                     WHERE e.tenant_id = :tenant_id AND e.src_event_id = :eid)""",
                eid=event_id,
                now=ctx.request_time,
            )

    def interpret_event(self, ctx: PrincipalContext, event: NaturalLanguageEvent) -> list[CandidateMemoryOperation]:
        """Dry run: what would the interpreter propose? Nothing is stored or committed."""
        ctx = require_context(ctx)
        return self._interpret(ctx, event)

    def _interpret(self, ctx: PrincipalContext, event: NaturalLanguageEvent) -> list[CandidateMemoryOperation]:
        interp = self.providers.interpreter
        try:
            proposals = interp.interpret(
                InterpreterInput(
                    event_type=event.event_type,
                    text=event.text,
                    actor_principal_id=ctx.principal_id,
                    observed_at=event.observed_at or ctx.request_time,
                )
            )
        except Exception:
            log.warning("interpreter_failed", extra={"interpreter": interp.name})
            return [NoMemoryChange()]
        return list(proposals) or [NoMemoryChange()]

    def ingest_natural_language(self, ctx: PrincipalContext, event: NaturalLanguageEvent) -> IngestResult:
        """Model-interpreted ingestion. Proposals are committed with origin=model,
        so high-impact or re-classifying operations are quarantined, never applied."""
        ctx = require_context(ctx)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            self._snapshot(tx, ctx)
            event_id = self._store_event(tx, ctx, event.event_type, event.observed_at, {"text": event.text})
        proposals = self._interpret(ctx, event)
        receipt = self.commit_operations(ctx, proposals, origin=Origin.MODEL, event_id=event_id)
        self._drop_unreferenced_payload(ctx, event_id)
        return IngestResult(
            event_id=event_id,
            interpreter=self.providers.interpreter.name,
            proposals=tuple(proposals),
            receipt=receipt,
        )

    def derive(self, ctx: PrincipalContext, req: DeriveRequest, *, origin: Origin = Origin.ORACLE) -> DeriveResult:
        ctx = require_context(ctx)
        receipt_id = new_id("rcpt")
        try:
            with self.db.tenant_tx(ctx.tenant_id) as tx:
                snap = self._snapshot(tx, ctx)
                w = WriteExecutor(tx, ctx, snap, self.engine, self.providers.embedder, origin)
                memory_id, version_id, reasons = derive_in_tx(w, req)
                result = OperationResult(
                    index=0,
                    op="ADD_FACT",
                    status=OperationStatus.COMMITTED,
                    reasons=tuple(r.value for r in reasons),
                    memory_id=memory_id,
                    version_id=version_id,
                )
                self._write_receipt(tx, ctx, receipt_id, "committed", [result])
                self.audit.record_decisions(tx, ctx, "derive", w.decisions)
        except OpRejected as rej:
            self._raise_for(rej.reasons)
        self.cache.invalidate_tenant(ctx.tenant_id)
        return DeriveResult(
            receipt_id=receipt_id,
            memory_id=memory_id,
            version_id=version_id,
            reason_codes=tuple(r.value for r in reasons),
            inherits_parent_policy=req.declassification is None,
        )

    @staticmethod
    def _raise_for(reasons: tuple[Reason, ...]) -> None:
        if Reason.NOT_FOUND in reasons:
            raise NotFoundError()
        if Reason.NOT_AUTHORIZED_TO_WRITE in reasons or Reason.MODEL_PROPOSAL_QUARANTINED in reasons:
            raise ForbiddenError(reasons[0])
        if Reason.STEP_UP_REQUIRED in reasons:
            raise ForbiddenError(Reason.STEP_UP_REQUIRED, "Stronger authentication or a declared purpose is required.")
        if Reason.VERSION_CONFLICT in reasons or Reason.SEMANTIC_IDENTITY_CONFLICT in reasons:
            raise ConflictError(reasons[0])
        raise ValidationFailure(reasons[0] if reasons else Reason.INVALID_OPERATION)

    # ================================================================ forget
    def forget(self, ctx: PrincipalContext, req: DeletionRequest) -> DeletionReceipt:
        ctx = require_context(ctx)
        receipt_id = new_id("rcpt")
        decisions: list[PolicyDecision] = []
        try:
            with self.db.tenant_tx(ctx.tenant_id) as tx:
                snap = self._snapshot(tx, ctx)
                outcome = tombstone_in_tx(tx, ctx, snap, self.engine, req.memory_ids, req.reason_code, decisions)
                result = OperationResult(
                    index=0,
                    op="DELETE_FACT",
                    status=OperationStatus.COMMITTED,
                    reasons=(Reason.DELETE_AUTHORIZED.value,),
                )
                self._write_receipt(tx, ctx, receipt_id, "committed", [result], memory_ids=req.memory_ids)
                self.audit.record_decisions(tx, ctx, "delete", decisions)
        except OpRejected as rej:
            with self.db.tenant_tx(ctx.tenant_id) as tx:
                self.audit.record_decisions(tx, ctx, "delete", decisions)
            self._raise_for(rej.reasons)
        self.cache.invalidate_tenant(ctx.tenant_id)
        statuses: list[CascadeStatus] = []
        if outcome.cascade_ids and self.settings.cascade_mode == "inline":
            statuses = self.cascades.run(ctx.tenant_id, outcome.cascade_ids)
        overall = (
            "completed"
            if statuses and all(s.status == "completed" for s in statuses)
            else ("not_required" if not outcome.cascade_ids else "pending")
        )
        return DeletionReceipt(
            receipt_id=receipt_id,
            correlation_id=ctx.correlation_id,
            memory_ids=tuple(sorted(req.memory_ids)),
            tombstone_ids=tuple(outcome.tombstone_ids),
            versions_tombstoned=outcome.versions_tombstoned,
            descendants_quarantined=outcome.descendants_quarantined,
            cascade_ids=tuple(outcome.cascade_ids),
            cascade_status=overall,
            policy_version=POLICY_ENGINE_VERSION,
            created_at=ctx.request_time,
        )

    def run_cascades(
        self, tenant_id: str, cascade_ids: list[str] | None = None, *, include_completed: bool = False
    ) -> list[CascadeStatus]:
        return self.cascades.run(tenant_id, cascade_ids, include_completed=include_completed)

    def get_cascade_status(self, ctx: PrincipalContext, cascade_id: str) -> CascadeStatus:
        ctx = require_context(ctx)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            owner = tx.one(
                """SELECT t.requested_by FROM deletion_cascades c
                   JOIN deletion_tombstones t ON t.tenant_id = c.tenant_id AND t.tombstone_id = c.tombstone_id
                   WHERE c.tenant_id = :tenant_id AND c.cascade_id = :cid""",
                cid=cascade_id,
            )
            if owner is None or owner["requested_by"] != ctx.principal_id:
                raise NotFoundError()
            status = cascade_status(tx, cascade_id)
        assert status is not None
        return status

    def get_receipt(self, ctx: PrincipalContext, receipt_id: str) -> dict[str, Any]:
        """Content-free receipt, visible only to the principal that caused it."""
        ctx = require_context(ctx)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            row = tx.one(
                """SELECT receipt_id, correlation_id, principal_id, status, operation_count, committed_count,
                          rejected_count, quarantined_count, memory_ids, reason_codes, policy_version, created_at
                   FROM commit_receipts WHERE tenant_id = :tenant_id AND receipt_id = :rid""",
                rid=receipt_id,
            )
            if row is None or row["principal_id"] != ctx.principal_id:
                raise NotFoundError()
            tombstones = tx.all(
                """SELECT t.tombstone_id, t.memory_id, t.cause, t.reason_code, t.versions_tombstoned,
                          c.cascade_id, c.status AS cascade_status
                   FROM deletion_tombstones t
                   LEFT JOIN deletion_cascades c ON c.tenant_id = t.tenant_id AND c.tombstone_id = t.tombstone_id
                   WHERE t.tenant_id = :tenant_id AND t.correlation_id = :cid""",
                cid=row["correlation_id"],
            )
            decisions = tx.all(
                """SELECT operation, memory_id, decision, view, reason_codes, policy_version, decided_at
                   FROM policy_decisions WHERE tenant_id = :tenant_id AND correlation_id = :cid
                   ORDER BY decided_at, decision_id""",
                cid=row["correlation_id"],
            )
        return {**dict(row), "tombstones": [dict(t) for t in tombstones], "decisions": [dict(d) for d in decisions]}

    # ================================================================= reads
    def _cache_key(self, ctx: PrincipalContext, req: QueryRequest, top_k: int) -> str:
        parts = [
            ctx.workspace_id,
            ctx.principal_id,
            ",".join(sorted(ctx.roles)),
            ctx.purpose or "",
            ctx.auth_strength.value,
            repr(sorted(ctx.attributes)),
            " ".join(req.query.lower().split()),
            str(top_k),
            req.as_of.isoformat() if req.as_of else "",
            req.max_view.value if req.max_view else "",
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def query(self, ctx: PrincipalContext, req: QueryRequest) -> QueryResponse:
        ctx = require_context(ctx)
        started = time.perf_counter()
        self.stats["queries"] += 1
        top_k = req.top_k or self.settings.top_k
        key = self._cache_key(ctx, req, top_k)
        cached = self.cache.get(ctx.tenant_id, key, ctx.request_time)
        if cached is not None:
            valid = self._still_valid(ctx, cached.plan, cached.as_of)
            if valid == cached.plan.approved_ids:
                self.stats["cache_hits"] += 1
                with self.db.tenant_tx(ctx.tenant_id) as tx:
                    self._snapshot(tx, ctx)
                    rid = self.audit.record_release(
                        tx,
                        ctx,
                        operation="query_cache",
                        action=cached.response.action,
                        approved=sorted(cached.plan.approved_ids),
                        blocked=[],
                        withheld_count=0,
                        reasons=list(cached.response.reason_codes),
                        policy_version=POLICY_ENGINE_VERSION,
                        query_text=req.query,
                        generator="cache",
                        confidence=None,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        prompt_tokens=0,
                        completion_tokens=0,
                    )
                return cached.response.model_copy(
                    update={"cache_hit": True, "release_id": rid, "correlation_id": ctx.correlation_id}
                )

        plan = self.providers.planner.plan(req.query, ctx, top_k=top_k, as_of=req.as_of, requested_view=req.max_view)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            snap = self._snapshot(tx, ctx)
            cands = list_authorized_candidates(tx, self.engine, ctx, snap, as_of=plan.as_of)
            self.audit.record_decisions(tx, ctx, "read", cands.decisions.values())
        if self.hooks.after_candidates:
            self.hooks.after_candidates(ctx, cands)
        bundle, refusal_relevant, escalation_relevant = self.retrieve(ctx, plan, cands, full_context=True)
        dplan = plan_disclosure(ctx, bundle, refusal_relevant=refusal_relevant, escalation_relevant=escalation_relevant)
        evidence = [item.for_generator() for item in bundle.items]
        if self.hooks.before_generate:
            self.hooks.before_generate(evidence)
        draft = self._generate(req.query, dplan, evidence)
        if self.hooks.before_release:
            self.hooks.before_release(ctx)
        release, final_plan = self._release(ctx, req.query, dplan, bundle, draft, plan.as_of)
        latency = int((time.perf_counter() - started) * 1000)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            release_id = self.audit.record_release(
                tx,
                ctx,
                operation="query",
                action=release.action,
                approved=release.approved_memory_ids,
                blocked=release.blocked_memory_ids,
                withheld_count=len(cands.refusable) + len(cands.escalations),
                reasons=release.reason_codes,
                policy_version=release.policy_version,
                query_text=req.query,
                generator=release.generator,
                confidence=release.confidence,
                latency_ms=latency,
                prompt_tokens=draft.prompt_tokens,
                completion_tokens=draft.completion_tokens,
            )
        approved = set(release.approved_memory_ids)
        public = tuple(
            _public(item) for item in bundle.items if item.memory_id in approved and release.action in ANSWER_ACTIONS
        )
        reason_codes = (
            (Reason.NO_USABLE_MEMORY.value,) if release.action is QueryAction.NO_MEMORY else release.reason_codes
        )
        response = QueryResponse(
            correlation_id=ctx.correlation_id,
            release_id=release_id,
            action=release.action,
            answer=release.content,
            evidence=public,
            reason_codes=reason_codes,
            policy_version=release.policy_version,
        )
        # Only clean releases are cached: anything blocked, re-rendered or flagged is recomputed.
        if release.action in ANSWER_ACTIONS and release.reason_codes == (Reason.VERIFIED.value,):
            self.cache.put(ctx.tenant_id, key, _Cached(response, final_plan, plan.as_of), ctx.request_time)
        return response

    def search(
        self, ctx: PrincipalContext, req: QueryRequest, *, full_context: bool = False
    ) -> tuple[PublicEvidence, ...]:
        """Governed retrieval without answer generation.

        ``full_context`` applies the answer path's budget (``full_context_max_items``): a permitted set
        that fits is returned whole instead of ranked.

        Returns exactly the permitted projections the query pipeline would hand a
        generator (same pre-filter, ranking and views), and audits the read
        decisions and the release. Nothing is generated, so nothing needs verifying.
        """
        ctx = require_context(ctx)
        started = time.perf_counter()
        plan = self.providers.planner.plan(
            req.query, ctx, top_k=req.top_k or self.settings.top_k, as_of=req.as_of, requested_view=req.max_view
        )
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            snap = self._snapshot(tx, ctx)
            cands = list_authorized_candidates(tx, self.engine, ctx, snap, as_of=plan.as_of)
            self.audit.record_decisions(tx, ctx, "search", cands.decisions.values())
        bundle, _, _ = self.retrieve(ctx, plan, cands, full_context=full_context)
        items = bundle.items
        if not items:
            action = QueryAction.NO_MEMORY
        elif all(i.view is View.FULL for i in items):
            action = QueryAction.ANSWER
        else:
            action = QueryAction.ANSWER_REDACTED
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            self.audit.record_release(
                tx,
                ctx,
                operation="search",
                action=action,
                approved=[i.memory_id for i in items],
                blocked=[],
                withheld_count=len(cands.refusable) + len(cands.escalations),
                reasons=[Reason.EVIDENCE_AVAILABLE.value if items else Reason.NO_USABLE_MEMORY.value],
                policy_version=POLICY_ENGINE_VERSION,
                query_text=req.query,
                generator="none",
                confidence=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
                prompt_tokens=0,
                completion_tokens=0,
            )
        return tuple(_public(i) for i in items)

    def retrieve(
        self, ctx: PrincipalContext, plan: QueryPlan, cands: CandidateSet, *, full_context: bool = False
    ) -> tuple[EvidenceBundle, bool, bool]:
        """Rank strictly inside the authorized set, then materialize permitted projections.

        With ``full_context`` (the answer path) and a configured budget, a permitted set
        that fits the budget is returned whole, oldest first, instead of ranked.
        """
        pairs: list[tuple[str, View]] = []
        policy_versions: dict[str, str] = {}
        for ref in cands.allowed:
            view = ref.view
            if plan.requested_view is not None and plan.requested_view.rank < view.rank:
                # A request may only lower the permitted view, never raise it.
                lowered = cap_view(plan.requested_view, ref.allowed_views)
                if lowered is None:
                    continue
                view = lowered
            pairs.append((ref.version_id, view))
            policy_versions[ref.memory_id] = ref.decision.policy_version
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            ranked = None
            if full_context and self.settings.full_context_max_items:
                ranked = all_within(
                    tx,
                    pairs,
                    max_items=self.settings.full_context_max_items,
                    max_chars=self.settings.full_context_max_chars,
                    dedup=self.ranking.dedup,
                    explain=self.hooks.on_ranking,
                )
            if ranked is None:
                ranked = rank_within(
                    tx, pairs, plan, self.ranking, self.providers.embedder, explain=self.hooks.on_ranking
                )
            refusal_relevant = escalation_relevant = False
            if not ranked:
                if cands.escalations:
                    escalation_relevant = bool(
                        rank_within(
                            tx,
                            [(r.version_id, View.EXISTENCE_ONLY) for r in cands.escalations],
                            plan,
                            self.ranking,
                            self.providers.embedder,
                            limit=1,
                        )
                    )
                if cands.refusable and not escalation_relevant:
                    refusal_relevant = bool(
                        rank_within(
                            tx,
                            [(r.version_id, View.EXISTENCE_ONLY) for r in cands.refusable],
                            plan,
                            self.ranking,
                            self.providers.embedder,
                            limit=1,
                        )
                    )
        items = tuple(
            EvidenceItem(
                memory_id=r.memory_id,
                version_id=r.version_id,
                view=r.view,
                semantic_type=r.semantic_type,
                content_schema=r.content_schema,
                topic=r.topic,
                text=r.text,
                fields=r.fields,
                observed_at=r.observed_at,
                valid_from=r.valid_from,
                valid_until=r.valid_until,
                version_state=r.version_state,
                truth_state=r.truth_state,
                is_derived=r.is_derived,
                score=r.score,
                lexical_hit=r.lexical_hit,
                vector_similarity=r.vector_similarity,
                policy_version=policy_versions.get(r.memory_id, POLICY_ENGINE_VERSION),
            )
            for r in ranked
        )
        return (
            EvidenceBundle(items=items, as_of=plan.as_of, authorized_candidates=len(pairs)),
            (refusal_relevant),
            escalation_relevant,
        )

    def _generate(self, query: str, plan: DisclosurePlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        gen = self.providers.generator
        try:
            return gen.generate(query, plan.for_generator(), evidence)
        except Exception:
            self.stats["generator_failures"] += 1
            log.warning("generator_failed", extra={"generator": gen.name})
            return self.fallback_generator.generate(query, plan.for_generator(), evidence)

    def _still_valid(self, ctx: PrincipalContext, plan: DisclosurePlan, as_of: datetime | None) -> frozenset[str]:
        """Live re-check at release time: each approved item must still be active,
        still the resolved version, and still authorized at no less than its view."""
        if not plan.items:
            return frozenset()
        now = max(ctx.request_time, self.clock.now())
        live = ctx.model_copy(update={"request_time": now})
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            snap = load_snapshot(tx, live)
            if not snap.principal_active:
                return frozenset()
            ids = set(plan.approved_ids)
            cands = list_authorized_candidates(
                tx, self.engine, live, snap, as_of=as_of, restrict_to=ids | ancestors(tx, ids)
            )
        by_id = {r.memory_id: r for r in cands.allowed}
        valid = {
            i.memory_id
            for i in plan.items
            if (r := by_id.get(i.memory_id)) is not None and r.version_id == i.version_id and r.view.rank >= i.view.rank
        }
        return frozenset(valid)

    def _protected_values(self, ctx: PrincipalContext, plan: DisclosurePlan, bundle: EvidenceBundle) -> ProtectedValues:
        values: list[str] = []
        by_id = {i.memory_id: i for i in bundle.items}
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            for item in plan.items:
                if item.view is View.FULL:
                    continue
                content = load_content(tx, item.version_id)
                ev = by_id.get(item.memory_id)
                if content is None or ev is None:
                    continue
                values.extend(hidden_values(get_schema(ev.content_schema), item.view, content))
        return ProtectedValues(values)

    def _release(
        self,
        ctx: PrincipalContext,
        query: str,
        plan: DisclosurePlan,
        bundle: EvidenceBundle,
        draft: DraftResponse,
        as_of: datetime | None,
    ) -> tuple[ReleaseDecision, DisclosurePlan]:
        still_valid = self._still_valid(ctx, plan, as_of)
        meta = ReleaseMetadata(
            tenant_id=ctx.tenant_id,
            protected=self._protected_values(ctx, plan, bundle),
            still_valid=still_valid,
            evidence_text=" ".join(i.text for i in bundle.items),
        )
        evidence = [i.for_generator() for i in bundle.items]
        reasons: list[str] = []
        final_plan = plan
        policy = self.settings.release_action_policy
        result = self.verifier.verify(draft, plan, meta, action_policy=policy)
        final = draft
        if result.ok and policy == "answerer" and draft.action not in ANSWER_ACTIONS:
            # The answerer declined. Release the canonical text for its action,
            # never free text, and approve no evidence.
            reasons.extend([Reason.VERIFIED.value, Reason.ANSWERER_DECLINED.value])
            final = DraftResponse(action=draft.action, text=CANONICAL_TEXT[draft.action], generator=draft.generator)
            final_plan = plan.model_copy(update={"items": (), "action": draft.action})
        elif result.ok:
            reasons.append(Reason.VERIFIED.value)
            if draft.action is not plan.action:
                final_plan = plan.model_copy(update={"action": draft.action})
        else:
            self.stats["release_blocks"] += 1
            reasons.extend(r.value for r in result.reasons)
            final, final_plan = self._safe_render(query, plan, still_valid, evidence, meta, reasons)
        confidence: float | None = None
        sv = self.providers.semantic_verifier
        if sv is not None and final.action in ANSWER_ACTIONS:
            try:
                verdict = sv.assess(final.text, final_plan.for_generator())
                confidence = verdict.confidence
                if verdict.block:
                    self.stats["semantic_blocks"] += 1
                    reasons.append(Reason.SEMANTIC_VERIFIER_BLOCK.value)
                    if final.generator != self.fallback_generator.name:
                        final, final_plan = self._safe_render(query, final_plan, still_valid, evidence, meta, reasons)
                    else:
                        final = DraftResponse(
                            action=QueryAction.REFUSE, text=CANONICAL_TEXT[QueryAction.REFUSE], generator="policy"
                        )
                        final_plan = final_plan.model_copy(update={"items": (), "action": QueryAction.REFUSE})
            except Exception:
                reasons.append(Reason.SEMANTIC_VERIFIER_UNAVAILABLE.value)
                if final.generator != self.fallback_generator.name:
                    final, final_plan = self._safe_render(query, final_plan, still_valid, evidence, meta, reasons)
        approved = tuple(sorted(final_plan.approved_ids)) if final.action in ANSWER_ACTIONS else ()
        blocked = tuple(sorted(plan.approved_ids - set(approved)))
        return (
            ReleaseDecision(
                action=final.action,
                content=final.text,
                approved_memory_ids=approved,
                blocked_memory_ids=blocked,
                reason_codes=tuple(dict.fromkeys(reasons)),
                confidence=confidence,
                policy_version=POLICY_ENGINE_VERSION,
                generator=final.generator,
            ),
            final_plan,
        )

    def _safe_render(
        self,
        query: str,
        plan: DisclosurePlan,
        still_valid: frozenset[str],
        evidence: Sequence[GeneratorEvidence],
        meta: ReleaseMetadata,
        reasons: list[str],
    ) -> tuple[DraftResponse, DisclosurePlan]:
        """Predetermined safe mechanism: re-render only still-valid projections
        deterministically; if nothing remains, return the canonical non-answer."""
        if plan.action not in ANSWER_ACTIONS:
            return (
                DraftResponse(action=plan.action, text=CANONICAL_TEXT[plan.action], generator="policy"),
                plan,
            )
        remaining = plan.restricted_to(still_valid)
        if remaining.action in ANSWER_ACTIONS:
            fb = self.fallback_generator.generate(
                query, remaining.for_generator(), [e for e in evidence if e.memory_id in still_valid]
            )
            if self.verifier.verify(fb, remaining, meta).ok:
                self.stats["fallback_renders"] += 1
                reasons.append(Reason.FALLBACK_DETERMINISTIC_RENDER.value)
                return fb, remaining
        empty = plan.model_copy(update={"items": (), "action": QueryAction.NO_MEMORY})
        return (
            DraftResponse(action=QueryAction.NO_MEMORY, text=CANONICAL_TEXT[QueryAction.NO_MEMORY], generator="policy"),
            empty,
        )

    def get_memory(
        self, ctx: PrincipalContext, memory_id: str, *, as_of: datetime | None = None, max_view: View | None = None
    ) -> MemoryView:
        """Governed direct lookup. Missing, cross-tenant, deleted and unauthorized
        ids are indistinguishable (uniform NotFoundError)."""
        ctx = require_context(ctx)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            snap = self._snapshot(tx, ctx)
            ids = {memory_id}
            cands = list_authorized_candidates(
                tx, self.engine, ctx, snap, as_of=as_of, restrict_to=ids | ancestors(tx, ids)
            )
            self.audit.record_decisions(
                tx, ctx, "direct_lookup", [d for mid, d in cands.decisions.items() if mid == memory_id]
            )
            ref = next((r for r in cands.allowed if r.memory_id == memory_id), None)
            if ref is None:
                raise NotFoundError()
            view = ref.view
            if max_view is not None and max_view.rank < view.rank:
                view = cap_view(max_view, ref.allowed_views) or view
            row = tx.one(
                """SELECT p.text, p.fields, a.semantic_type, a.topic, a.observed_at, a.is_derived,
                          v.valid_from, v.valid_until, v.state, v.truth_state
                   FROM memory_projections p
                   JOIN memory_versions v ON v.tenant_id = p.tenant_id AND v.version_id = p.version_id
                   JOIN memory_atoms a ON a.tenant_id = p.tenant_id AND a.memory_id = p.memory_id
                   WHERE p.tenant_id = :tenant_id AND p.version_id = :vid AND p.view = :view
                     AND a.lifecycle_state = 'active' AND v.state <> 'tombstoned'""",
                vid=ref.version_id,
                view=view.value,
            )
            if row is None:
                raise NotFoundError()
            self.audit.record_release(
                tx,
                ctx,
                operation="direct_lookup",
                action=QueryAction.ANSWER if view is View.FULL else QueryAction.ANSWER_REDACTED,
                approved=[memory_id],
                blocked=[],
                withheld_count=0,
                reasons=[Reason.EVIDENCE_AVAILABLE.value],
                policy_version=ref.decision.policy_version,
                query_text=memory_id,
                generator="direct",
                confidence=None,
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
            )
        return MemoryView(
            memory_id=memory_id,
            version_id=ref.version_id,
            view=view,
            semantic_type=row["semantic_type"],
            topic=row["topic"],
            text=row["text"],
            fields=dict(row["fields"] or {}),
            observed_at=row["observed_at"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            version_state=row["state"],
            truth_state=row["truth_state"],
            is_derived=bool(row["is_derived"]),
        )

    def get_history(self, ctx: PrincipalContext, memory_id: str) -> list[MemoryView]:
        """All non-tombstoned versions the requester may see. Superseded versions
        require historical authorization (``policy.historical_access``)."""
        ctx = require_context(ctx)
        with self.db.tenant_tx(ctx.tenant_id) as tx:
            snap = self._snapshot(tx, ctx)
            res = resolve(tx, self.engine, ctx, snap, memory_id, current=False)
            if res is None:
                raise NotFoundError()
            meta = tx.one(
                """SELECT semantic_type, topic, observed_at, is_derived FROM memory_atoms
                   WHERE tenant_id = :tenant_id AND memory_id = :mid""",
                mid=memory_id,
            )
            versions = tx.all(
                """SELECT version_id, state AS version_state, valid_from, valid_until, truth_state
                   FROM memory_versions
                   WHERE tenant_id = :tenant_id AND memory_id = :mid AND state <> 'tombstoned'
                   ORDER BY valid_from, created_at""",
                mid=memory_id,
            )
            out: list[MemoryView] = []
            decisions: list[PolicyDecision] = []
            base = load_atom_meta(tx, memory_id)
            assert base is not None
            for v in versions:
                facts = facts_from_row({**base, **v}, res.facts.parent_ids)
                historical = v["version_state"] != "active"
                d = self.engine.evaluate_read(
                    ctx,
                    facts,
                    snap,
                    at=v["valid_from"],
                    historical=historical,
                    parent_decisions=res.parent_decisions,
                )
                decisions.append(d)
                if not d.allowed or d.view is None:
                    continue
                p = tx.one(
                    """SELECT text, fields FROM memory_projections
                       WHERE tenant_id = :tenant_id AND version_id = :vid AND view = :view""",
                    vid=v["version_id"],
                    view=d.view.value,
                )
                if p is None or meta is None:
                    continue
                out.append(
                    MemoryView(
                        memory_id=memory_id,
                        version_id=v["version_id"],
                        view=d.view,
                        semantic_type=meta["semantic_type"],
                        topic=meta["topic"],
                        text=p["text"],
                        fields=dict(p["fields"] or {}),
                        observed_at=meta["observed_at"],
                        valid_from=v["valid_from"],
                        valid_until=v["valid_until"],
                        version_state=v["version_state"],
                        truth_state=v["truth_state"],
                        is_derived=bool(meta["is_derived"]),
                    )
                )
            self.audit.record_decisions(tx, ctx, "history", decisions)
        if not out:
            raise NotFoundError()
        return out

    # ============================================================ maintenance
    def audit_export(self, tenant_id: str, *, limit: int = 200) -> dict[str, Any]:
        """Content-free audit export for administrators."""
        with self.db.tenant_tx(tenant_id) as tx:
            decisions = tx.all(
                """SELECT decision_id, correlation_id, principal_id, operation, memory_id, decision, view, reason_codes,
                          policy_version, decided_at
                   FROM policy_decisions WHERE tenant_id = :tenant_id ORDER BY decided_at DESC, decision_id LIMIT :n""",
                n=limit,
            )
            releases = tx.all(
                """SELECT release_id, correlation_id, principal_id, operation, action, approved_memory_ids,
                          blocked_memory_ids, withheld_count, reason_codes, policy_version, query_fingerprint,
                          generator, verifier_confidence, latency_ms, prompt_tokens, completion_tokens, created_at
                   FROM release_audits WHERE tenant_id = :tenant_id ORDER BY created_at DESC, release_id LIMIT :n""",
                n=limit,
            )
            tombstones = tx.all(
                """SELECT tombstone_id, memory_id, requested_by, reason_code, cause, parent_tombstone_id,
                          correlation_id, versions_tombstoned, policy_version, created_at
                   FROM deletion_tombstones WHERE tenant_id = :tenant_id ORDER BY created_at DESC LIMIT :n""",
                n=limit,
            )
            cascades = tx.all(
                """SELECT cascade_id, tombstone_id, status, attempts, descendants_found, descendants_tombstoned,
                          versions_scrubbed, projections_deleted, embeddings_deleted, raw_events_scrubbed,
                          cache_evicted, last_error_code, created_at, completed_at
                   FROM deletion_cascades WHERE tenant_id = :tenant_id ORDER BY created_at DESC LIMIT :n""",
                n=limit,
            )
            receipts = tx.all(
                """SELECT receipt_id, correlation_id, principal_id, status, operation_count, committed_count,
                          rejected_count, quarantined_count, memory_ids, reason_codes, policy_version, created_at
                   FROM commit_receipts WHERE tenant_id = :tenant_id ORDER BY created_at DESC LIMIT :n""",
                n=limit,
            )
        return {
            "policy_decisions": [dict(r) for r in decisions],
            "release_audits": [dict(r) for r in releases],
            "deletion_tombstones": [dict(r) for r in tombstones],
            "deletion_cascades": [dict(r) for r in cascades],
            "commit_receipts": [dict(r) for r in receipts],
        }

    def reindex(self, tenant_id: str) -> int:
        """Rebuild index material from non-tombstoned versions only. Tombstoned
        content cannot be restored: it is scrubbed, filtered out here, and a
        database trigger rejects index rows for tombstoned versions."""
        n = 0
        now = self.clock.now()
        with self.db.tenant_tx(tenant_id) as tx:
            rows = tx.all(
                """SELECT v.version_id, v.memory_id, a.content_schema, a.topic, a.semantic_type, a.allowed_views
                   FROM memory_versions v
                   JOIN memory_atoms a ON a.tenant_id = v.tenant_id AND a.memory_id = v.memory_id
                   WHERE v.tenant_id = :tenant_id AND v.state <> 'tombstoned'
                     AND a.lifecycle_state IN ('active', 'expired') AND v.content_scrubbed_at IS NULL"""
            )
            tx.exec("DELETE FROM memory_projections WHERE tenant_id = :tenant_id")
            for r in rows:
                content = load_content(tx, r["version_id"])
                if content is None:
                    continue
                index_rows = build_index_rows(
                    get_schema(r["content_schema"]),
                    content,
                    topic=r["topic"],
                    semantic_type=r["semantic_type"],
                    allowed_views=[View(v) for v in r["allowed_views"]],
                    embedder=self.providers.embedder,
                )
                insert_index_rows(
                    tx,
                    memory_id=r["memory_id"],
                    version_id=r["version_id"],
                    rows=index_rows,
                    embedder=self.providers.embedder,
                    now=now,
                )
                n += 1
        self.cache.invalidate_tenant(tenant_id)
        return n

    def system_context(self, tenant_id: str, workspace_id: str, principal_id: str) -> PrincipalContext:
        """Context for trusted internal jobs (never for requests)."""
        now = self.clock.now()
        _, roles = self.roles_for(tenant_id, principal_id, now)
        return PrincipalContext(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            roles=roles,
            auth_time=now,
            auth_strength=AuthStrength.MFA,
            request_time=now,
            correlation_id=new_id("corr"),
            issued_by="system",
        )
