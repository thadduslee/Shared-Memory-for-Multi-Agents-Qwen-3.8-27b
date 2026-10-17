"""Authorized candidate construction (pre-retrieval enforcement).

Stage A (SQL, metadata only): an index-backed *superset* of memories the
requester could possibly read, restricted to the tenant and workspace, to
active lifecycle, to unexpired retention, to a version valid at the requested
time, and excluding explicit denials. Access is approximated with
``owner_ids``, ``access_tags``/``existence_tags`` GIN lookups and live grants.

Stage B (in-process): the deterministic engine evaluates every Stage A row
exactly. Only ALLOW / ALLOW_REDACTED rows become retrievable candidates.
Derived memories that inherit policy are admitted in later rounds, and only
when *every* provenance parent was authorized in an earlier round.

No content column is read in either stage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gsm.db.database import Tx
from gsm.domain.context import PrincipalContext
from gsm.domain.types import Decision, VersionState, View
from gsm.memory.repo import ATOM_META_COLUMNS, DERIVATION_EDGE_SQL, facts_from_row, parent_map
from gsm.policy.decision import PolicyDecision
from gsm.policy.engine import PolicyEngine
from gsm.policy.facts import AuthSnapshot
from gsm.policy.tags import requester_tags

MAX_DERIVATION_DEPTH = 4

_CURRENT_VERSION = "mv.state = 'active' AND mv.valid_from <= :at AND (mv.valid_until IS NULL OR mv.valid_until > :at)"
_AS_OF_VERSION = (
    "mv.state IN ('active', 'superseded', 'expired') AND mv.valid_from <= :at "
    "AND (mv.valid_until IS NULL OR mv.valid_until > :at)"
)

_STAGE_A = """
SELECT {meta}, v.version_id, v.state AS version_state, v.valid_from, v.valid_until
FROM memory_atoms a
JOIN LATERAL (
    SELECT mv.version_id, mv.state, mv.valid_from, mv.valid_until
    FROM memory_versions mv
    WHERE mv.tenant_id = a.tenant_id AND mv.memory_id = a.memory_id
      AND mv.state <> 'tombstoned' AND {version_predicate}
    ORDER BY mv.valid_from DESC, mv.created_at DESC
    LIMIT 1
) v ON true
WHERE a.tenant_id = :tenant_id
  AND a.workspace_id = :workspace_id
  AND a.lifecycle_state = 'active'
  AND (a.retain_until IS NULL OR a.retain_until > :now)
  AND a.inherits_parent_policy = :inheriting
  AND (CAST(:restrict AS text[]) IS NULL OR a.memory_id = ANY(CAST(:restrict AS text[])))
  AND NOT (a.memory_id = ANY(CAST(:already AS text[])))
  AND NOT EXISTS (
      SELECT 1 FROM explicit_denials d
      WHERE d.tenant_id = a.tenant_id AND d.principal_id = :principal_id AND d.lifted_at IS NULL
        AND d.valid_from <= :now AND (d.valid_until IS NULL OR d.valid_until > :now)
        AND ((d.resource_type = 'memory' AND d.resource_id = a.memory_id)
          OR (d.resource_type = 'compartment' AND d.resource_id = ANY(a.compartments))
          OR (d.resource_type = 'subject' AND d.resource_id = ANY(a.subject_ids))))
  AND ({access})
"""

_OWN_ACCESS = """
    a.owner_ids @> ARRAY[CAST(:principal_id AS text)]
    OR a.access_tags && CAST(:tags AS text[])
    OR a.existence_tags && CAST(:tags AS text[])
    OR EXISTS (
        SELECT 1 FROM explicit_grants g
        WHERE g.tenant_id = a.tenant_id AND g.memory_id = a.memory_id
          AND g.grantee_principal_id = :principal_id AND g.revoked_at IS NULL
          AND g.valid_from <= :now AND (g.expires_at IS NULL OR g.expires_at > :now))
"""

_INHERITED_ACCESS = f"""
    EXISTS (
        SELECT 1 FROM memory_edges e
        WHERE e.tenant_id = a.tenant_id AND e.dst_memory_id = a.memory_id
          AND e.edge_type IN {DERIVATION_EDGE_SQL})
    AND NOT EXISTS (
        SELECT 1 FROM memory_edges e
        WHERE e.tenant_id = a.tenant_id AND e.dst_memory_id = a.memory_id
          AND e.edge_type IN {DERIVATION_EDGE_SQL}
          AND NOT (e.src_memory_id = ANY(CAST(:authorized AS text[]))))
"""


@dataclass(frozen=True)
class MemoryReference:
    memory_id: str
    version_id: str
    view: View
    decision: PolicyDecision
    historical: bool
    allowed_views: frozenset[View]


@dataclass
class CandidateSet:
    allowed: list[MemoryReference] = field(default_factory=list)
    # DENY with existence acknowledgement: rankable only on their existence label.
    refusable: list[MemoryReference] = field(default_factory=list)
    # Would be allowed after step-up / declaring a purpose.
    escalations: list[MemoryReference] = field(default_factory=list)
    decisions: dict[str, PolicyDecision] = field(default_factory=dict)
    stage_a_count: int = 0

    @property
    def allowed_ids(self) -> set[str]:
        return {r.memory_id for r in self.allowed}


def list_authorized_candidates(
    tx: Tx,
    engine: PolicyEngine,
    ctx: PrincipalContext,
    snap: AuthSnapshot,
    *,
    as_of: datetime | None = None,
    restrict_to: Iterable[str] | None = None,
) -> CandidateSet:
    now = ctx.request_time
    at = as_of or now
    version_predicate = _AS_OF_VERSION if as_of is not None else _CURRENT_VERSION
    tags = requester_tags(ctx, snap)
    restrict = sorted(set(restrict_to)) if restrict_to is not None else None
    out = CandidateSet()

    def evaluate(rows: Sequence[Mapping[str, Any]], parents: dict[str, list[str]] | None) -> None:
        for row in rows:
            facts = facts_from_row(row, (parents or {}).get(row["memory_id"], ()))
            historical = row["version_state"] != VersionState.ACTIVE.value or (
                row["valid_until"] is not None and row["valid_until"] <= now
            )
            decision = engine.evaluate_read(
                ctx, facts, snap, at=at, historical=historical, parent_decisions=out.decisions
            )
            out.decisions[facts.memory_id] = decision
            ref_view = decision.view or View.EXISTENCE_ONLY
            ref = MemoryReference(
                facts.memory_id, row["version_id"], ref_view, decision, historical, facts.allowed_views
            )
            if decision.allowed:
                out.allowed.append(ref)
            elif decision.decision is Decision.ESCALATE:
                out.escalations.append(ref)
            elif decision.decision is Decision.DENY and decision.existence_ack:
                out.refusable.append(ref)

    base = {
        "workspace_id": ctx.workspace_id,
        "principal_id": ctx.principal_id,
        "now": now,
        "at": at,
        "restrict": restrict,
    }
    rows = tx.all(
        _STAGE_A.format(meta=ATOM_META_COLUMNS, version_predicate=version_predicate, access=_OWN_ACCESS),
        inheriting=False,
        tags=tags,
        already=[],
        **base,
    )
    out.stage_a_count += len(rows)
    evaluate(rows, None)

    for _ in range(MAX_DERIVATION_DEPTH):
        rows = tx.all(
            _STAGE_A.format(meta=ATOM_META_COLUMNS, version_predicate=version_predicate, access=_INHERITED_ACCESS),
            inheriting=True,
            authorized=sorted(out.allowed_ids),
            already=sorted(out.decisions),
            **base,
        )
        if not rows:
            break
        out.stage_a_count += len(rows)
        evaluate(rows, parent_map(tx, [r["memory_id"] for r in rows]))
    return out
