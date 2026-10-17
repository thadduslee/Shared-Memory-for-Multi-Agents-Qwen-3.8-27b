"""Single-memory decisions that respect provenance inheritance.

Derived memories that inherit policy are decided from the *live* decisions on
their parents, so revoking access to (or deleting) any parent immediately
revokes access to every summary built from it.
"""

from __future__ import annotations

from dataclasses import dataclass

from gsm.db.database import Tx
from gsm.domain.context import PrincipalContext
from gsm.memory.repo import current_version, facts_from_row, latest_version, load_atom_meta, parent_map
from gsm.policy.decision import PolicyDecision
from gsm.policy.engine import PolicyEngine, WriteAction
from gsm.policy.facts import AuthSnapshot, MemoryFacts

MAX_LINEAGE_DEPTH = 8


@dataclass(frozen=True)
class Resolved:
    facts: MemoryFacts
    parent_decisions: dict[str, PolicyDecision]


def resolve(
    tx: Tx,
    engine: PolicyEngine,
    ctx: PrincipalContext,
    snap: AuthSnapshot,
    memory_id: str,
    *,
    current: bool,
    lock: bool = False,
    _depth: int = 0,
) -> Resolved | None:
    """Load facts for ``memory_id`` plus live read decisions for its parents.

    ``current=True`` resolves the version valid now; ``False`` resolves the
    latest non-tombstoned version (used by lifecycle writes such as delete).
    Returns None when the memory does not exist in this tenant.
    """
    row = load_atom_meta(tx, memory_id, for_update=lock)
    if row is None:
        return None
    version = current_version(tx, memory_id, ctx.request_time) if current else latest_version(tx, memory_id)
    parents: list[str] = []
    parent_decisions: dict[str, PolicyDecision] = {}
    if row["inherits_parent_policy"] and _depth < MAX_LINEAGE_DEPTH:
        parents = parent_map(tx, [memory_id]).get(memory_id, [])
        for pid in parents:
            decision = read_decision(tx, engine, ctx, snap, pid, _depth=_depth + 1)
            if decision is not None:
                parent_decisions[pid] = decision
    merged = {**row, **(version or {"version_id": None})}
    return Resolved(facts_from_row(merged, parents), parent_decisions)


def read_decision(
    tx: Tx, engine: PolicyEngine, ctx: PrincipalContext, snap: AuthSnapshot, memory_id: str, *, _depth: int = 0
) -> PolicyDecision | None:
    res = resolve(tx, engine, ctx, snap, memory_id, current=True, _depth=_depth)
    if res is None:
        return None
    return engine.evaluate_read(ctx, res.facts, snap, parent_decisions=res.parent_decisions)


def write_decision(
    tx: Tx,
    engine: PolicyEngine,
    ctx: PrincipalContext,
    snap: AuthSnapshot,
    memory_id: str,
    action: WriteAction,
    *,
    lock: bool = True,
) -> tuple[PolicyDecision, Resolved] | None:
    current = action is not WriteAction.DELETE
    res = resolve(tx, engine, ctx, snap, memory_id, current=current, lock=lock)
    if res is None:
        return None
    decision = engine.authorize_write(
        ctx,
        res.facts,
        snap,
        action,
        parent_decisions=res.parent_decisions,
        require_version=current,
    )
    return decision, res
