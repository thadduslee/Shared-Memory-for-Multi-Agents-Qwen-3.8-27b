"""Synchronous deletion (tombstoning).

Inside one transaction: resolve exact targets, authorize each one, tombstone
every version (current *and* historical), tombstone the atom, move all
derivation descendants to ``pending_invalidation`` so they are unavailable
immediately, write content-free tombstones, and enqueue cascade jobs.

After commit, every retrieval path excludes the memory because each of them
requires ``lifecycle_state = 'active'`` and a non-tombstoned version.
Physical cleanup (scrubbing content, index material, embeddings, raw events)
is the cascade's job; see ``gsm.memory.cascade``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from gsm.db.database import Tx
from gsm.domain.context import PrincipalContext
from gsm.domain.reasons import Reason
from gsm.domain.types import Decision
from gsm.ids import new_id
from gsm.memory.errors import OpRejected
from gsm.memory.repo import descendants
from gsm.policy.decision import PolicyDecision
from gsm.policy.engine import PolicyEngine, WriteAction
from gsm.policy.facts import AuthSnapshot
from gsm.policy.lineage import write_decision


@dataclass
class TombstoneOutcome:
    memory_ids: list[str] = field(default_factory=list)
    tombstone_ids: list[str] = field(default_factory=list)
    versions_tombstoned: int = 0
    descendants_quarantined: int = 0
    cascade_ids: list[str] = field(default_factory=list)
    already_deleted: list[str] = field(default_factory=list)


def tombstone_in_tx(
    tx: Tx,
    ctx: PrincipalContext,
    snap: AuthSnapshot,
    engine: PolicyEngine,
    memory_ids: Sequence[str],
    reason_code: str,
    decisions: list[PolicyDecision],
) -> TombstoneOutcome:
    ids = sorted(set(memory_ids))
    if not ids:
        raise OpRejected(Reason.AMBIGUOUS_TARGET)
    out = TombstoneOutcome()
    targets: list[str] = []
    for mid in ids:
        state = tx.one(
            """SELECT a.lifecycle_state, t.tombstone_id, t.requested_by
               FROM memory_atoms a
               LEFT JOIN deletion_tombstones t ON t.tenant_id = a.tenant_id AND t.memory_id = a.memory_id
               WHERE a.tenant_id = :tenant_id AND a.memory_id = :mid""",
            mid=mid,
        )
        if state is None:
            raise OpRejected(Reason.NOT_FOUND)
        if state["lifecycle_state"] == "tombstoned":
            # Idempotent repeat by the original requester; everyone else gets NOT_FOUND.
            if state["requested_by"] == ctx.principal_id:
                out.already_deleted.append(mid)
                if state["tombstone_id"]:
                    out.tombstone_ids.append(state["tombstone_id"])
                continue
            raise OpRejected(Reason.NOT_FOUND)
        result = write_decision(tx, engine, ctx, snap, mid, WriteAction.DELETE, lock=True)
        if result is None:
            raise OpRejected(Reason.NOT_FOUND)
        decision = result[0]
        decisions.append(decision)
        if not decision.allowed:
            if decision.decision is Decision.ESCALATE:
                raise OpRejected(Reason.STEP_UP_REQUIRED)
            if Reason.NOT_AUTHORIZED_TO_WRITE in decision.reasons or decision.existence_ack:
                raise OpRejected(Reason.NOT_AUTHORIZED_TO_WRITE)
            raise OpRejected(Reason.NOT_FOUND)
        targets.append(mid)

    now = ctx.request_time
    for mid in targets:
        n = tx.exec(
            """UPDATE memory_versions SET state = 'tombstoned', tombstoned_at = :now
               WHERE tenant_id = :tenant_id AND memory_id = :mid AND state <> 'tombstoned'""",
            mid=mid,
            now=now,
        )
        tx.exec(
            """UPDATE memory_atoms SET lifecycle_state = 'tombstoned', tombstoned_at = :now
               WHERE tenant_id = :tenant_id AND memory_id = :mid AND lifecycle_state <> 'tombstoned'""",
            mid=mid,
            now=now,
        )
        tombstone_id = new_id("tomb")
        tx.exec(
            """INSERT INTO deletion_tombstones (tenant_id, tombstone_id, memory_id, requested_by, reason_code, cause,
                   correlation_id, versions_tombstoned, policy_version, created_at)
               VALUES (:tenant_id, :tid, :mid, :by, :reason, 'direct', :cid, :n, :pv, :now)""",
            tid=tombstone_id,
            mid=mid,
            by=ctx.principal_id,
            reason=reason_code,
            cid=ctx.correlation_id,
            n=n,
            pv=engine.policy_version(None),
            now=now,
        )
        cascade_id = new_id("casc")
        tx.exec(
            """INSERT INTO deletion_cascades (tenant_id, cascade_id, tombstone_id, status, created_at, updated_at)
               VALUES (:tenant_id, :cid, :tid, 'pending', :now, :now)""",
            cid=cascade_id,
            tid=tombstone_id,
            now=now,
        )
        out.memory_ids.append(mid)
        out.tombstone_ids.append(tombstone_id)
        out.cascade_ids.append(cascade_id)
        out.versions_tombstoned += n
    desc = [mid for mid, _depth in descendants(tx, targets)]
    if desc:
        out.descendants_quarantined = tx.exec(
            """UPDATE memory_atoms SET lifecycle_state = 'pending_invalidation'
               WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))
                 AND lifecycle_state = 'active'""",
            ids=desc,
        )
    return out
