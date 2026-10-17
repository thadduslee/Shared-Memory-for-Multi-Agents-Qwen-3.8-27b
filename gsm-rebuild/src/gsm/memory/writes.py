"""Validation, authorization and transactional application of memory operations.

Every operation is authorized by the deterministic engine inside the same
transaction that applies it. Any rejection raises ``OpRejected``; the caller
rolls back the *whole* batch so lifecycle and index state never diverge.

Quarantine (triage) is not a failure: a model-proposed high-impact operation,
a model re-classification, or a low-confidence change to a sensitive record is
reported back unapplied and must be re-submitted by a trusted caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gsm.db.database import Tx, jsonb
from gsm.disclosure.schemas import ContentValidationError, get_schema, validate_content, validate_topic
from gsm.domain.content import Content
from gsm.domain.context import PrincipalContext
from gsm.domain.operations import (
    AddFact,
    AddRelationship,
    CandidateMemoryOperation,
    DeleteFact,
    GrantAccess,
    NewFact,
    NoMemoryChange,
    OperationResult,
    PolicyTemplateRef,
    RemoveRelationship,
    RestrictAccess,
    RetireFact,
    RevokeAccess,
    SupersedeFact,
    UpdateFact,
)
from gsm.domain.policy import MemoryPolicy, is_at_least_as_restrictive
from gsm.domain.reasons import Reason
from gsm.domain.types import (
    HIGH_IMPACT_OPS,
    Decision,
    OperationStatus,
    Origin,
    Sensitivity,
    View,
)
from gsm.ids import new_id
from gsm.memory import forget
from gsm.memory.errors import OpRejected
from gsm.memory.repo import build_index_rows, insert_index_rows, load_content
from gsm.policy import templates
from gsm.policy.decision import PolicyDecision
from gsm.policy.engine import PolicyEngine, WriteAction
from gsm.policy.facts import AuthSnapshot
from gsm.policy.lineage import write_decision
from gsm.policy.tags import access_tags, existence_tags
from gsm.providers.base import Embedder

LOW_CONFIDENCE_THRESHOLD = 0.7
DELEGABLE_RELATIONS = frozenset({"member_of", "assigned_to"})
ADMIN_ROLE = "admin"


def resolve_policy(fact: NewFact) -> MemoryPolicy:
    if fact.policy is not None:
        return fact.policy
    ref = fact.policy_template or PolicyTemplateRef(name="private")
    if ref.name == "private":
        return templates.private()
    if ref.name == "hr_restricted":
        return templates.hr_restricted()
    if ref.name == "tenant_public":
        return templates.tenant_public()
    if ref.project_id is None:
        raise OpRejected(Reason.INVALID_OPERATION)
    if ref.name == "project":
        return templates.project(ref.project_id)
    return templates.availability_record(ref.project_id)


def semantic_key(fact: NewFact, owners: tuple[str, ...]) -> str | None:
    """Semantic identity of a single-valued fact.

    (semantic_type, sorted subject_ids, attribute, sorted owner_ids). The owner
    namespace means a principal's write can never collide with, and therefore
    never reveal, a record owned by someone else.
    """
    if fact.attribute is None:
        return None
    return "|".join(
        [fact.semantic_type.value, ",".join(sorted(fact.subject_ids)), fact.attribute, ",".join(sorted(owners))]
    )


def raise_for_write(decision: PolicyDecision) -> None:
    """Map a write decision to an outcome that never reveals invisible records.

    A requester who cannot see a memory gets NOT_FOUND - the same answer as for
    an id that does not exist or belongs to another tenant.
    """
    if decision.allowed:
        return
    if decision.decision is Decision.UNAVAILABLE or (
        decision.decision is Decision.DENY
        and Reason.NOT_AUTHORIZED_TO_WRITE not in decision.reasons
        and not decision.existence_ack
    ):
        raise OpRejected(Reason.NOT_FOUND)
    if decision.decision is Decision.ESCALATE:
        raise OpRejected(Reason.STEP_UP_REQUIRED)
    raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)


@dataclass
class WriteExecutor:
    tx: Tx
    ctx: PrincipalContext
    snap: AuthSnapshot
    engine: PolicyEngine
    embedder: Embedder | None
    origin: Origin
    event_id: str | None = None
    decisions: list[PolicyDecision] = field(default_factory=list)
    tombstoned: list[str] = field(default_factory=list)
    cascade_ids: list[str] = field(default_factory=list)

    @property
    def now(self) -> datetime:
        return self.ctx.request_time

    # ----------------------------------------------------------------- triage
    def triage(self, op: CandidateMemoryOperation) -> tuple[Reason, ...] | None:
        """Reasons to quarantine ``op`` without applying it, or None."""
        if self.origin is Origin.MODEL:
            if op.op in HIGH_IMPACT_OPS:
                return (Reason.MODEL_PROPOSAL_QUARANTINED, Reason.HIGH_IMPACT_REQUIRES_CONFIRMATION)
            if isinstance(op, UpdateFact) and (
                op.declassify
                or op.sensitivity is not None
                or op.compartments is not None
                or op.policy is not None
                or op.allowed_views is not None
            ):
                return (Reason.MODEL_PROPOSAL_QUARANTINED, Reason.SENSITIVITY_DOWNGRADE)
            if isinstance(op, AddFact | SupersedeFact):
                fact = op.fact if isinstance(op, AddFact) else op.replacement
                if fact.policy is not None or fact.owner_ids is not None:
                    return (Reason.MODEL_PROPOSAL_QUARANTINED, Reason.POLICY_BROADENING)
        if isinstance(op, AddFact | SupersedeFact):
            fact = op.fact if isinstance(op, AddFact) else op.replacement
            if op.confidence < LOW_CONFIDENCE_THRESHOLD and fact.sensitivity.rank >= Sensitivity.CONFIDENTIAL.rank:
                return (Reason.LOW_CONFIDENCE_SENSITIVE,)
        return None

    # ------------------------------------------------------------------ apply
    def apply(self, index: int, op: CandidateMemoryOperation) -> OperationResult:
        quarantine = self.triage(op)
        if quarantine is not None:
            return OperationResult(
                index=index, op=op.op, status=OperationStatus.QUARANTINED, reasons=tuple(r.value for r in quarantine)
            )
        if isinstance(op, NoMemoryChange):
            return OperationResult(index=index, op=op.op, status=OperationStatus.NOOP, reasons=(Reason.NO_CHANGE,))
        handler = {
            AddFact: self._add,
            UpdateFact: self._update,
            SupersedeFact: self._supersede,
            RetireFact: self._retire,
            RestrictAccess: self._restrict,
            DeleteFact: self._delete,
            GrantAccess: self._grant,
            RevokeAccess: self._revoke,
            AddRelationship: self._add_relationship,
            RemoveRelationship: self._remove_relationship,
        }.get(type(op))
        if handler is None:
            raise OpRejected(Reason.UNSUPPORTED_OPERATION)
        result: dict[str, Any] = handler(op)  # type: ignore[operator]
        status = result.pop("status", OperationStatus.COMMITTED)
        reasons = tuple(r.value for r in result.pop("reasons", (Reason.WRITE_AUTHORIZED,)))
        return OperationResult(index=index, op=op.op, status=status, reasons=reasons, **result)

    # --------------------------------------------------------------- helpers
    def _authorize(self, memory_id: str, action: WriteAction) -> tuple[PolicyDecision, Mapping[str, Any]]:
        out = write_decision(self.tx, self.engine, self.ctx, self.snap, memory_id, action)
        if out is None:
            raise OpRejected(Reason.NOT_FOUND)
        decision, res = out
        self.decisions.append(decision)
        raise_for_write(decision)
        row = self.tx.one(
            """SELECT memory_id, content_schema, semantic_type, topic, owner_ids, subject_ids, sensitivity,
                      compartments, policy, allowed_views, inherits_parent_policy, is_derived, semantic_key,
                      attribute
               FROM memory_atoms WHERE tenant_id = :tenant_id AND memory_id = :mid""",
            mid=memory_id,
        )
        assert row is not None
        return decision, {**row, "_facts": res.facts}

    def _principals_exist(self, ids: tuple[str, ...]) -> bool:
        if not ids:
            return True
        n = self.tx.scalar(
            """SELECT count(*) FROM principals
               WHERE tenant_id = :tenant_id AND principal_id = ANY(CAST(:ids AS text[])) AND active""",
            ids=list(set(ids)),
        )
        return int(n or 0) == len(set(ids))

    def _insert_atom(
        self,
        *,
        memory_id: str,
        fact: NewFact,
        owners: tuple[str, ...],
        policy: MemoryPolicy,
        skey: str | None,
    ) -> None:
        subjects = sorted(set(fact.subject_ids))
        self.tx.exec(
            """INSERT INTO memory_atoms (tenant_id, workspace_id, memory_id, semantic_type, content_schema, attribute,
                   semantic_key, topic, subject_ids, owner_ids, source_event_id, source_principal_id, sensitivity,
                   compartments, policy, allowed_views, access_tags, existence_tags, lifecycle_state, retain_until,
                   observed_at, created_at)
               VALUES (:tenant_id, :ws, :mid, :stype, :schema, :attr, :skey, :topic, CAST(:subjects AS text[]),
                   CAST(:owners AS text[]), :eid, :src, :sens, CAST(:comps AS text[]), CAST(:policy AS jsonb),
                   CAST(:views AS text[]), CAST(:atags AS text[]), CAST(:etags AS text[]), 'active', :retain,
                   :observed, :now)""",
            ws=self.ctx.workspace_id,
            mid=memory_id,
            stype=fact.semantic_type.value,
            schema=fact.content_schema,
            attr=fact.attribute,
            skey=skey,
            topic=fact.topic,
            subjects=subjects,
            owners=list(owners),
            eid=self.event_id,
            src=self.ctx.principal_id,
            sens=fact.sensitivity.value,
            comps=sorted(fact.compartments),
            policy=policy.model_dump_json(),
            views=sorted(v.value for v in fact.allowed_views),
            atags=access_tags(policy, subjects),
            etags=existence_tags(policy, subjects),
            retain=fact.retain_until,
            observed=fact.observed_at or self.now,
            now=self.now,
        )

    def _insert_version(
        self,
        *,
        memory_id: str,
        content: Content,
        skey: str | None,
        valid_from: datetime,
        valid_until: datetime | None,
        confidence: float,
        supersedes: str | None,
        origin: Origin,
    ) -> str:
        version_id = new_id("ver")
        self.tx.exec(
            """INSERT INTO memory_versions (tenant_id, workspace_id, version_id, memory_id, semantic_key, content_text,
                   content_fields, valid_from, valid_until, state, truth_state, supersedes_version_id,
                   extraction_confidence, origin, created_by, created_at)
               VALUES (:tenant_id, :ws, :vid, :mid, :skey, :text, CAST(:fields AS jsonb), :vf, :vu, 'active',
                   'current', :sup, :conf, :origin, :by, :now)""",
            ws=self.ctx.workspace_id,
            vid=version_id,
            mid=memory_id,
            skey=skey,
            text=content.text,
            fields=jsonb(content.fields),
            vf=valid_from,
            vu=valid_until,
            sup=supersedes,
            conf=confidence,
            origin=origin.value,
            by=self.ctx.principal_id,
            now=self.now,
        )
        return version_id

    def _index(
        self,
        *,
        memory_id: str,
        version_id: str,
        schema_name: str,
        content: Content,
        topic: str,
        semantic_type: str,
        views: frozenset[View],
    ) -> None:
        rows = build_index_rows(
            get_schema(schema_name),
            content,
            topic=topic,
            semantic_type=semantic_type,
            allowed_views=views,
            embedder=self.embedder,
        )
        insert_index_rows(
            self.tx, memory_id=memory_id, version_id=version_id, rows=rows, embedder=self.embedder, now=self.now
        )

    def add_fact(self, fact: NewFact, confidence: float, *, memory_id: str | None = None) -> tuple[str, str]:
        try:
            schema = get_schema(fact.content_schema)
            validate_content(schema, fact.content)
        except ContentValidationError:
            raise OpRejected(Reason.INVALID_OPERATION) from None
        try:
            validate_topic(schema, fact.content, fact.topic)
        except ContentValidationError:
            raise OpRejected(Reason.TOPIC_CONTAINS_PROTECTED_VALUE) from None
        owners = tuple(sorted(set(fact.owner_ids or (self.ctx.principal_id,))))
        if self.origin is Origin.MODEL:
            owners = (self.ctx.principal_id,)
        if self.ctx.principal_id not in owners:
            raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
        if not self._principals_exist(owners):
            raise OpRejected(Reason.INVALID_OPERATION)
        policy = resolve_policy(fact)
        skey = semantic_key(fact, owners)
        if skey is not None:
            self.tx.exec(
                """UPDATE memory_versions SET state = 'expired'
                   WHERE tenant_id = :tenant_id AND workspace_id = :ws AND semantic_key = :k AND state = 'active'
                     AND valid_until IS NOT NULL AND valid_until <= :now""",
                ws=self.ctx.workspace_id,
                k=skey,
                now=self.now,
            )
            clash = self.tx.one(
                """SELECT memory_id FROM memory_versions
                   WHERE tenant_id = :tenant_id AND workspace_id = :ws AND semantic_key = :k AND state = 'active'""",
                ws=self.ctx.workspace_id,
                k=skey,
            )
            if clash is not None:
                raise OpRejected(Reason.SEMANTIC_IDENTITY_CONFLICT)
        memory_id = memory_id or new_id("mem")
        self._insert_atom(memory_id=memory_id, fact=fact, owners=owners, policy=policy, skey=skey)
        version_id = self._insert_version(
            memory_id=memory_id,
            content=fact.content,
            skey=skey,
            valid_from=fact.valid_from or self.now,
            valid_until=fact.valid_until,
            confidence=confidence,
            supersedes=None,
            origin=self.origin,
        )
        self._index(
            memory_id=memory_id,
            version_id=version_id,
            schema_name=fact.content_schema,
            content=fact.content,
            topic=fact.topic,
            semantic_type=fact.semantic_type.value,
            views=fact.allowed_views,
        )
        if self.event_id is not None:
            self.tx.exec(
                """INSERT INTO memory_edges (tenant_id, edge_id, edge_type, src_event_id, dst_memory_id, created_at)
                   VALUES (:tenant_id, :id, 'source_event', :eid, :mid, :now)""",
                id=new_id("edge"),
                eid=self.event_id,
                mid=memory_id,
                now=self.now,
            )
        return memory_id, version_id

    def _supersede_current(
        self, memory_id: str, expected: str | None, valid_from: datetime, *, contradiction: bool = False
    ) -> Mapping[str, Any]:
        cur = self.tx.one(
            """SELECT version_id, valid_from, valid_until, semantic_key FROM memory_versions
               WHERE tenant_id = :tenant_id AND memory_id = :mid AND state = 'active'
               FOR UPDATE""",
            mid=memory_id,
        )
        if cur is None or (expected is not None and cur["version_id"] != expected):
            raise OpRejected(Reason.VERSION_CONFLICT)
        if valid_from < cur["valid_from"]:
            raise OpRejected(Reason.INVALID_OPERATION)
        n = self.tx.exec(
            """UPDATE memory_versions
               SET state = 'superseded', truth_state = :truth,
                   valid_until = CASE WHEN valid_until IS NULL OR valid_until > :vf THEN :vf ELSE valid_until END
               WHERE tenant_id = :tenant_id AND version_id = :vid AND state = 'active'""",
            truth="contradicted" if contradiction else "superseded",
            vf=valid_from,
            vid=cur["version_id"],
        )
        if n != 1:
            raise OpRejected(Reason.VERSION_CONFLICT)
        return cur

    # ------------------------------------------------------------- handlers
    def _add(self, op: AddFact) -> dict[str, Any]:
        memory_id, version_id = self.add_fact(op.fact, op.confidence)
        return {"memory_id": memory_id, "version_id": version_id}

    def _update(self, op: UpdateFact) -> dict[str, Any]:
        _decision, atom = self._authorize(op.memory_id, WriteAction.UPDATE)
        if atom["inherits_parent_policy"]:
            raise OpRejected(Reason.INVALID_OPERATION)
        schema = get_schema(atom["content_schema"])
        try:
            validate_content(schema, op.content)
        except ContentValidationError:
            raise OpRejected(Reason.INVALID_OPERATION) from None
        old_sens = Sensitivity(atom["sensitivity"])
        if op.confidence < LOW_CONFIDENCE_THRESHOLD and old_sens.rank >= Sensitivity.CONFIDENTIAL.rank:
            return {
                "status": OperationStatus.QUARANTINED,
                "reasons": (Reason.LOW_CONFIDENCE_SENSITIVE,),
                "memory_id": op.memory_id,
            }
        reasons: list[Reason] = [Reason.WRITE_AUTHORIZED]
        views = frozenset(View(v) for v in atom["allowed_views"])
        reclassify = any(x is not None for x in (op.sensitivity, op.compartments, op.policy, op.allowed_views))
        if reclassify:
            old_policy = MemoryPolicy.model_validate(atom["policy"])
            new_policy = op.policy or old_policy
            new_sens = op.sensitivity or old_sens
            new_comps = frozenset(op.compartments) if op.compartments is not None else frozenset(atom["compartments"])
            new_views = frozenset(op.allowed_views) if op.allowed_views is not None else views
            if not new_views:
                raise OpRejected(Reason.INVALID_OPERATION)
            safe = is_at_least_as_restrictive(
                new_policy,
                old_policy,
                new_sensitivity=new_sens,
                old_sensitivity=old_sens,
                new_compartments=new_comps,
                old_compartments=frozenset(atom["compartments"]),
                new_views=new_views,
                old_views=views,
            )
            if not safe:
                if not op.declassify:
                    raise OpRejected(
                        Reason.SENSITIVITY_DOWNGRADE if new_sens.rank < old_sens.rank else Reason.POLICY_BROADENING
                    )
                if self.ctx.principal_id not in set(atom["owner_ids"]) or old_policy.owner_view is None:
                    raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
                reasons.append(Reason.DECLASSIFIED)
            subjects = sorted(atom["subject_ids"])
            self.tx.exec(
                """UPDATE memory_atoms
                   SET sensitivity = :sens, compartments = CAST(:comps AS text[]), policy = CAST(:policy AS jsonb),
                       allowed_views = CAST(:views AS text[]), access_tags = CAST(:atags AS text[]),
                       existence_tags = CAST(:etags AS text[]), policy_revision = policy_revision + 1
                   WHERE tenant_id = :tenant_id AND memory_id = :mid""",
                sens=new_sens.value,
                comps=sorted(new_comps),
                policy=new_policy.model_dump_json(),
                views=sorted(v.value for v in new_views),
                atags=access_tags(new_policy, subjects),
                etags=existence_tags(new_policy, subjects),
                mid=op.memory_id,
            )
            self._reproject_history(op.memory_id, atom, new_views)
            views = new_views
        valid_from = op.valid_from or self.now
        old = self._supersede_current(op.memory_id, op.expected_version_id, valid_from, contradiction=op.contradiction)
        version_id = self._insert_version(
            memory_id=op.memory_id,
            content=op.content,
            skey=old["semantic_key"],
            valid_from=valid_from,
            valid_until=None,
            confidence=op.confidence,
            supersedes=old["version_id"],
            origin=self.origin,
        )
        self._index(
            memory_id=op.memory_id,
            version_id=version_id,
            schema_name=atom["content_schema"],
            content=op.content,
            topic=atom["topic"],
            semantic_type=atom["semantic_type"],
            views=views,
        )
        return {"memory_id": op.memory_id, "version_id": version_id, "reasons": tuple(reasons)}

    def _reproject_history(self, memory_id: str, atom: Mapping[str, Any], views: frozenset[View]) -> None:
        """Keep index material for older versions consistent with the new allowed views."""
        keep = sorted({v.value for v in views} | {View.EXISTENCE_ONLY.value})
        self.tx.exec(
            """DELETE FROM memory_projections
               WHERE tenant_id = :tenant_id AND memory_id = :mid AND NOT (view = ANY(CAST(:keep AS text[])))""",
            mid=memory_id,
            keep=keep,
        )
        versions = self.tx.all(
            """SELECT v.version_id, array_agg(p.view) FILTER (WHERE p.view IS NOT NULL) AS have
               FROM memory_versions v
               LEFT JOIN memory_projections p ON p.tenant_id = v.tenant_id AND p.version_id = v.version_id
               WHERE v.tenant_id = :tenant_id AND v.memory_id = :mid AND v.state <> 'tombstoned'
               GROUP BY v.version_id""",
            mid=memory_id,
        )
        schema = get_schema(atom["content_schema"])
        for v in versions:
            missing = frozenset(View(x) for x in keep) - frozenset(View(x) for x in (v["have"] or []))
            if not missing:
                continue
            content = load_content(self.tx, v["version_id"])
            if content is None:
                continue
            rows = build_index_rows(
                schema,
                content,
                topic=atom["topic"],
                semantic_type=atom["semantic_type"],
                allowed_views=missing,
                embedder=self.embedder,
            )
            rows = [r for r in rows if r.view in missing]
            insert_index_rows(
                self.tx,
                memory_id=memory_id,
                version_id=v["version_id"],
                rows=rows,
                embedder=self.embedder,
                now=self.now,
            )

    def _supersede(self, op: SupersedeFact) -> dict[str, Any]:
        _decision, atom = self._authorize(op.memory_id, WriteAction.UPDATE)
        if atom["inherits_parent_policy"]:
            raise OpRejected(Reason.INVALID_OPERATION)
        is_owner = self.ctx.principal_id in set(atom["owner_ids"])
        if not is_owner:
            new_policy = resolve_policy(op.replacement)
            safe = is_at_least_as_restrictive(
                new_policy,
                MemoryPolicy.model_validate(atom["policy"]),
                new_sensitivity=op.replacement.sensitivity,
                old_sensitivity=Sensitivity(atom["sensitivity"]),
                new_compartments=frozenset(op.replacement.compartments),
                old_compartments=frozenset(atom["compartments"]),
                new_views=frozenset(op.replacement.allowed_views),
                old_views=frozenset(View(v) for v in atom["allowed_views"]),
            )
            if not safe:
                raise OpRejected(Reason.POLICY_BROADENING)
        valid_from = op.replacement.valid_from or self.now
        self._supersede_current(op.memory_id, op.expected_version_id, valid_from)
        new_mid = new_id("mem")
        replacement = op.replacement.model_copy(update={"valid_from": valid_from})
        mid, vid = self.add_fact(replacement, op.confidence, memory_id=new_mid)
        self.tx.exec(
            "UPDATE memory_atoms SET superseded_by_memory_id = :new WHERE tenant_id = :tenant_id AND memory_id = :old",
            new=mid,
            old=op.memory_id,
        )
        self.tx.exec(
            """INSERT INTO memory_edges (tenant_id, edge_id, edge_type, src_memory_id, dst_memory_id, created_at)
               VALUES (:tenant_id, :id, 'supersedes', :old, :new, :now)""",
            id=new_id("edge"),
            old=op.memory_id,
            new=mid,
            now=self.now,
        )
        return {"memory_id": mid, "version_id": vid}

    def _retire(self, op: RetireFact) -> dict[str, Any]:
        _decision, atom = self._authorize(op.memory_id, WriteAction.UPDATE)
        if atom["inherits_parent_policy"]:
            raise OpRejected(Reason.INVALID_OPERATION)
        cur = self._supersede_current(op.memory_id, op.expected_version_id, self.now)
        return {"memory_id": op.memory_id, "version_id": cur["version_id"]}

    def _delete(self, op: DeleteFact) -> dict[str, Any]:
        ids = list(op.memory_ids)
        if op.selector is not None:
            ids = self._resolve_selector(op)
        outcome = forget.tombstone_in_tx(
            self.tx, self.ctx, self.snap, self.engine, ids, "USER_REQUESTED_DELETION", self.decisions
        )
        self.tombstoned.extend(outcome.memory_ids)
        self.cascade_ids.extend(outcome.cascade_ids)
        return {"memory_id": ids[0] if len(ids) == 1 else None, "reasons": (Reason.DELETE_AUTHORIZED,)}

    def _resolve_selector(self, op: DeleteFact) -> list[str]:
        sel = op.selector
        assert sel is not None
        rows = self.tx.all(
            """SELECT memory_id FROM memory_atoms
               WHERE tenant_id = :tenant_id AND workspace_id = :ws AND lifecycle_state = 'active'
                 AND subject_ids @> ARRAY[CAST(:sid AS text)]
                 AND (CAST(:stype AS text) IS NULL OR semantic_type = :stype)
                 AND (CAST(:attr AS text) IS NULL OR attribute = :attr)
               ORDER BY memory_id""",
            ws=self.ctx.workspace_id,
            sid=sel.subject_id,
            stype=sel.semantic_type.value if sel.semantic_type else None,
            attr=sel.attribute,
        )
        deletable: list[str] = []
        for r in rows:
            out = write_decision(
                self.tx, self.engine, self.ctx, self.snap, r["memory_id"], WriteAction.DELETE, lock=False
            )
            if out is not None and out[0].allowed:
                deletable.append(r["memory_id"])
        # Only memories the caller may delete are counted, so the count cannot
        # reveal records the caller cannot see.
        if not deletable:
            raise OpRejected(Reason.NOT_FOUND)
        if op.expected_count is None and len(deletable) > 1:
            raise OpRejected(Reason.AMBIGUOUS_TARGET)
        if op.expected_count is not None and op.expected_count != len(deletable):
            raise OpRejected(Reason.AMBIGUOUS_TARGET)
        return deletable

    def _grant(self, op: GrantAccess) -> dict[str, Any]:
        _decision, atom = self._authorize(op.memory_id, WriteAction.GRANT)
        if atom["inherits_parent_policy"]:
            raise OpRejected(Reason.INVALID_OPERATION)
        if op.view.value not in set(atom["allowed_views"]):
            raise OpRejected(Reason.VIEW_NOT_ALLOWED)
        if not self._principals_exist((op.grantee_principal_id,)):
            raise OpRejected(Reason.INVALID_OPERATION)
        if op.expires_at is not None and op.expires_at <= self.now:
            raise OpRejected(Reason.INVALID_OPERATION)
        if self.ctx.principal_id not in set(atom["owner_ids"]):
            read = self.engine.evaluate_read(self.ctx, atom["_facts"], self.snap)
            if read.view is None or op.view.rank > read.view.rank:
                raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
        grant_id = new_id("grant")
        self.tx.exec(
            """INSERT INTO explicit_grants (tenant_id, grant_id, resource_type, memory_id, grantee_principal_id, view,
                   purposes, valid_from, expires_at, granted_by, created_at)
               VALUES (:tenant_id, :gid, 'memory', :mid, :grantee, :view, CAST(:purposes AS text[]), :now, :exp,
                   :by, :now)""",
            gid=grant_id,
            mid=op.memory_id,
            grantee=op.grantee_principal_id,
            view=op.view.value,
            purposes=sorted(op.purposes),
            exp=op.expires_at,
            by=self.ctx.principal_id,
            now=self.now,
        )
        return {"memory_id": op.memory_id, "grant_id": grant_id}

    def _restrict(self, op: RestrictAccess) -> dict[str, Any]:
        _decision, atom = self._authorize(op.memory_id, WriteAction.GRANT if op.lift else WriteAction.UPDATE)
        if not self._principals_exist(op.principal_ids):
            raise OpRejected(Reason.INVALID_OPERATION)
        targets = sorted(set(op.principal_ids) - set(atom["owner_ids"]))
        if op.lift:
            n = self.tx.exec(
                """UPDATE explicit_denials SET lifted_at = :now
                   WHERE tenant_id = :tenant_id AND resource_type = 'memory' AND resource_id = :mid
                     AND principal_id = ANY(CAST(:pids AS text[])) AND lifted_at IS NULL
                     AND reason_code = :code""",
                now=self.now,
                mid=op.memory_id,
                pids=targets,
                code=Reason.RESTRICTED_BY_PRINCIPAL.value,
            )
        else:
            n = 0
            for pid in targets:
                active = self.tx.scalar(
                    """SELECT count(*) FROM explicit_denials
                       WHERE tenant_id = :tenant_id AND resource_type = 'memory' AND resource_id = :mid
                         AND principal_id = :pid AND lifted_at IS NULL
                         AND (valid_until IS NULL OR valid_until > :now)""",
                    mid=op.memory_id,
                    pid=pid,
                    now=self.now,
                )
                if int(active or 0):
                    continue
                self.tx.exec(
                    """INSERT INTO explicit_denials (tenant_id, denial_id, resource_type, resource_id, principal_id,
                           reason_code, valid_from, created_by, created_at)
                       VALUES (:tenant_id, :id, 'memory', :mid, :pid, :code, :now, :by, :now)""",
                    id=new_id("deny"),
                    mid=op.memory_id,
                    pid=pid,
                    code=Reason.RESTRICTED_BY_PRINCIPAL.value,
                    by=self.ctx.principal_id,
                    now=self.now,
                )
                n += 1
        if not n:
            return {"memory_id": op.memory_id, "status": OperationStatus.NOOP, "reasons": (Reason.NO_CHANGE,)}
        return {"memory_id": op.memory_id}

    def _revoke(self, op: RevokeAccess) -> dict[str, Any]:
        g = self.tx.one(
            """SELECT grant_id, resource_type, memory_id, grantee_principal_id, granted_by, revoked_at
               FROM explicit_grants WHERE tenant_id = :tenant_id AND grant_id = :gid FOR UPDATE""",
            gid=op.grant_id,
        )
        if g is None:
            raise OpRejected(Reason.NOT_FOUND)
        me = self.ctx.principal_id
        allowed = me in (g["granted_by"], g["grantee_principal_id"]) or ADMIN_ROLE in self.ctx.roles
        if not allowed and g["resource_type"] == "memory":
            out = write_decision(self.tx, self.engine, self.ctx, self.snap, g["memory_id"], WriteAction.GRANT)
            if out is not None:
                self.decisions.append(out[0])
                raise_for_write(out[0])
                allowed = True
        if not allowed:
            raise OpRejected(Reason.NOT_FOUND)
        if g["revoked_at"] is not None:
            return {"grant_id": op.grant_id, "status": OperationStatus.NOOP, "reasons": (Reason.NO_CHANGE,)}
        self.tx.exec(
            """UPDATE explicit_grants SET revoked_at = :now, revoked_by = :me
               WHERE tenant_id = :tenant_id AND grant_id = :gid AND revoked_at IS NULL""",
            now=self.now,
            me=me,
            gid=op.grant_id,
        )
        return {"grant_id": op.grant_id, "memory_id": g["memory_id"]}

    def _relationship_authority(self, relation: str, object_type: str, object_id: str) -> bool:
        if ADMIN_ROLE in self.ctx.roles:
            return True
        return relation in DELEGABLE_RELATIONS and self.snap.has_relation("owner_of", object_type, object_id, self.now)

    def _add_relationship(self, op: AddRelationship) -> dict[str, Any]:
        if not self._relationship_authority(op.relation, op.object_type, op.object_id):
            raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
        if not self._principals_exist((op.subject_principal_id,)):
            raise OpRejected(Reason.INVALID_OPERATION)
        if op.valid_until is not None and op.valid_until <= self.now:
            raise OpRejected(Reason.INVALID_OPERATION)
        existing = self.tx.one(
            """SELECT relationship_id FROM relationships
               WHERE tenant_id = :tenant_id AND subject_principal_id = :s AND relation = :r AND object_type = :ot
                 AND object_id = :oid AND removed_at IS NULL""",
            s=op.subject_principal_id,
            r=op.relation,
            ot=op.object_type,
            oid=op.object_id,
        )
        if existing is not None:
            return {
                "relationship_id": existing["relationship_id"],
                "status": OperationStatus.NOOP,
                "reasons": (Reason.NO_CHANGE,),
            }
        rid = new_id("rel")
        insert_relationship(
            self.tx,
            rid,
            op.subject_principal_id,
            op.relation,
            op.object_type,
            op.object_id,
            self.now,
            op.valid_until,
            self.ctx.principal_id,
        )
        return {"relationship_id": rid}

    def _remove_relationship(self, op: RemoveRelationship) -> dict[str, Any]:
        r = self.tx.one(
            """SELECT relationship_id, subject_principal_id, relation, object_type, object_id, removed_at
               FROM relationships WHERE tenant_id = :tenant_id AND relationship_id = :rid FOR UPDATE""",
            rid=op.relationship_id,
        )
        if r is None:
            raise OpRejected(Reason.NOT_FOUND)
        own = r["subject_principal_id"] == self.ctx.principal_id
        if not own and not self._relationship_authority(r["relation"], r["object_type"], r["object_id"]):
            raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
        if r["removed_at"] is not None:
            return {
                "relationship_id": op.relationship_id,
                "status": OperationStatus.NOOP,
                "reasons": (Reason.NO_CHANGE,),
            }
        self.tx.exec(
            """UPDATE relationships SET removed_at = :now
               WHERE tenant_id = :tenant_id AND relationship_id = :rid AND removed_at IS NULL""",
            now=self.now,
            rid=op.relationship_id,
        )
        return {"relationship_id": op.relationship_id}


def insert_relationship(
    tx: Tx,
    relationship_id: str,
    subject: str,
    relation: str,
    object_type: str,
    object_id: str,
    valid_from: datetime,
    valid_until: datetime | None,
    created_by: str,
) -> None:
    tx.exec(
        """INSERT INTO relationships (tenant_id, relationship_id, subject_principal_id, relation, object_type,
               object_id, valid_from, valid_until, created_by, created_at)
           VALUES (:tenant_id, :rid, :s, :r, :ot, :oid, :vf, :vu, :by, :vf)""",
        rid=relationship_id,
        s=subject,
        r=relation,
        ot=object_type,
        oid=object_id,
        vf=valid_from,
        vu=valid_until,
        by=created_by,
    )
