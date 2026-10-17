"""Deletion cascade: provenance invalidation plus physical cleanup.

For each pending job (one per direct tombstone):

1. traverse all derivation descendants (derived_from / summary_of / report_of);
2. decide each descendant through a ``DerivedRecomputer``. The MVP rule is
   conservative: *any tombstoned parent tombstones the derived memory*;
3. scrub content from every version of every tombstoned memory in the closure,
   delete their projections (lexical index material) and embeddings, blank the
   topic label, revoke grants on them, and scrub the payload of raw events
   that sourced them;
4. evict the application cache and mark the job completed with counts only.

Every step is idempotent (guards on current state, ``ON CONFLICT DO NOTHING``),
so re-running a job, or running it concurrently with ``SKIP LOCKED``, is safe.
Failure rolls the step back and records only the exception *type*.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from gsm.clock import Clock
from gsm.db.database import Database, Tx
from gsm.domain.reasons import Reason
from gsm.ids import new_id
from gsm.memory.repo import descendants, parent_map

CASCADE_ACTOR = "system:cascade"


class DerivedRecomputer(Protocol):
    """Decides the fate of a derived memory whose lineage includes a deletion.

    A future implementation can return "recompute" and rebuild the memory from
    its remaining valid parents; the MVP never does.
    """

    def decide(
        self, memory_id: str, parent_ids: list[str], tombstoned_parents: int
    ) -> Literal["tombstone", "keep"]: ...


class ConservativeRecomputer:
    def decide(self, memory_id: str, parent_ids: list[str], tombstoned_parents: int) -> Literal["tombstone", "keep"]:
        return "tombstone" if tombstoned_parents > 0 or not parent_ids else "keep"


@dataclass(frozen=True)
class CascadeStatus:
    cascade_id: str
    tombstone_id: str
    status: str
    attempts: int
    descendants_found: int
    descendants_tombstoned: int
    versions_scrubbed: int
    projections_deleted: int
    embeddings_deleted: int
    raw_events_scrubbed: int
    cache_evicted: bool
    last_error_code: str | None


def cascade_status(tx: Tx, cascade_id: str) -> CascadeStatus | None:
    r = tx.one(
        """SELECT cascade_id, tombstone_id, status, attempts, descendants_found, descendants_tombstoned,
                  versions_scrubbed, projections_deleted, embeddings_deleted, raw_events_scrubbed, cache_evicted,
                  last_error_code
           FROM deletion_cascades WHERE tenant_id = :tenant_id AND cascade_id = :cid""",
        cid=cascade_id,
    )
    return CascadeStatus(**dict(r)) if r else None


class CascadeRunner:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        evict: Callable[[str], object],
        recomputer: DerivedRecomputer | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.evict = evict
        self.recomputer = recomputer or ConservativeRecomputer()

    def run(
        self, tenant_id: str, cascade_ids: list[str] | None = None, *, include_completed: bool = False
    ) -> list[CascadeStatus]:
        with self.db.tenant_tx(tenant_id) as tx:
            if cascade_ids is None:
                states = ["pending", "failed", "running"] + (["completed"] if include_completed else [])
                rows = tx.all(
                    """SELECT cascade_id FROM deletion_cascades
                       WHERE tenant_id = :tenant_id AND status = ANY(CAST(:states AS text[]))
                       ORDER BY created_at, cascade_id""",
                    states=states,
                )
                cascade_ids = [r["cascade_id"] for r in rows]
        out: list[CascadeStatus] = []
        for cid in cascade_ids:
            status = self._run_one(tenant_id, cid)
            if status is not None:
                out.append(status)
        return out

    def _run_one(self, tenant_id: str, cascade_id: str) -> CascadeStatus | None:
        now = self.clock.now()
        try:
            with self.db.tenant_tx(tenant_id) as tx:
                job = tx.one(
                    """SELECT c.cascade_id, c.tombstone_id, t.memory_id
                       FROM deletion_cascades c
                       JOIN deletion_tombstones t ON t.tenant_id = c.tenant_id AND t.tombstone_id = c.tombstone_id
                       WHERE c.tenant_id = :tenant_id AND c.cascade_id = :cid
                       FOR UPDATE OF c SKIP LOCKED""",
                    cid=cascade_id,
                )
                if job is None:
                    return cascade_status(tx, cascade_id)
                counts = self._work(tx, job["memory_id"], job["tombstone_id"], now)
                self.evict(tenant_id)
                tx.exec(
                    """UPDATE deletion_cascades SET
                           status = 'completed', attempts = attempts + 1,
                           descendants_found = :df,
                           descendants_tombstoned = descendants_tombstoned + :dt,
                           versions_scrubbed = versions_scrubbed + :vs,
                           projections_deleted = projections_deleted + :pd,
                           embeddings_deleted = embeddings_deleted + :ed,
                           raw_events_scrubbed = raw_events_scrubbed + :rs,
                           cache_evicted = true, last_error_code = NULL,
                           updated_at = :now, completed_at = COALESCE(completed_at, :now)
                       WHERE tenant_id = :tenant_id AND cascade_id = :cid""",
                    cid=cascade_id,
                    now=now,
                    **counts,
                )
            self.evict(tenant_id)
        except Exception as exc:
            with self.db.tenant_tx(tenant_id) as tx:
                tx.exec(
                    """UPDATE deletion_cascades SET status = 'failed', attempts = attempts + 1,
                           last_error_code = :code, updated_at = :now
                       WHERE tenant_id = :tenant_id AND cascade_id = :cid""",
                    code=type(exc).__name__[:64],
                    now=now,
                    cid=cascade_id,
                )
        with self.db.tenant_tx(tenant_id) as tx:
            return cascade_status(tx, cascade_id)

    def _work(self, tx: Tx, root: str, tombstone_id: str, now: object) -> dict[str, int]:
        desc = descendants(tx, [root])
        tombstoned_now = 0
        for mid, _depth in desc:  # ordered by depth: parents are decided before children
            atom = tx.one(
                """SELECT lifecycle_state FROM memory_atoms
                   WHERE tenant_id = :tenant_id AND memory_id = :mid FOR UPDATE""",
                mid=mid,
            )
            if atom is None or atom["lifecycle_state"] == "tombstoned":
                continue
            parents = parent_map(tx, [mid]).get(mid, [])
            dead = int(
                tx.scalar(
                    """SELECT count(*) FROM memory_atoms
                       WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))
                         AND lifecycle_state = 'tombstoned'""",
                    ids=parents,
                )
                or 0
            )
            if self.recomputer.decide(mid, parents, dead) == "tombstone":
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
                tx.exec(
                    """INSERT INTO deletion_tombstones (tenant_id, tombstone_id, memory_id, requested_by, reason_code,
                           cause, parent_tombstone_id, correlation_id, versions_tombstoned, policy_version,
                           created_at)
                       VALUES (:tenant_id, :tid, :mid, :by, :reason, 'cascade', :parent, :cid, :n, 'cascade/v1', :now)
                       ON CONFLICT (tenant_id, memory_id) DO NOTHING""",
                    tid=new_id("tomb"),
                    mid=mid,
                    by=CASCADE_ACTOR,
                    reason=Reason.CASCADE_PARENT_DELETED.value,
                    parent=tombstone_id,
                    cid=tombstone_id,
                    n=n,
                    now=now,
                )
                tombstoned_now += 1
            else:
                tx.exec(
                    """UPDATE memory_atoms SET lifecycle_state = 'active'
                       WHERE tenant_id = :tenant_id AND memory_id = :mid
                         AND lifecycle_state = 'pending_invalidation'""",
                    mid=mid,
                )
        closure = [root] + [mid for mid, _ in desc]
        dead_ids = [
            r["memory_id"]
            for r in tx.all(
                """SELECT memory_id FROM memory_atoms
                   WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))
                     AND lifecycle_state = 'tombstoned'""",
                ids=closure,
            )
        ]
        vs = tx.exec(
            """UPDATE memory_versions
               SET content_text = NULL, content_fields = NULL, content_scrubbed_at = :now
               WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))
                 AND state = 'tombstoned' AND content_scrubbed_at IS NULL""",
            ids=dead_ids,
            now=now,
        )
        ed = tx.exec(
            "DELETE FROM embeddings WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))",
            ids=dead_ids,
        )
        pd = tx.exec(
            "DELETE FROM memory_projections WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))",
            ids=dead_ids,
        )
        tx.exec(
            """UPDATE memory_atoms SET topic = '[deleted]'
               WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[])) AND topic <> '[deleted]'""",
            ids=dead_ids,
        )
        tx.exec(
            """UPDATE explicit_grants SET revoked_at = :now, revoked_by = :by
               WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[])) AND revoked_at IS NULL""",
            ids=dead_ids,
            now=now,
            by=CASCADE_ACTOR,
        )
        rs = tx.exec(
            """UPDATE raw_events SET payload = NULL, payload_state = 'scrubbed', payload_scrubbed_at = :now
               WHERE tenant_id = :tenant_id AND payload_state = 'present' AND event_id IN (
                   SELECT src_event_id FROM memory_edges
                   WHERE tenant_id = :tenant_id AND edge_type = 'source_event'
                     AND dst_memory_id = ANY(CAST(:ids AS text[]))
                   UNION
                   SELECT source_event_id FROM memory_atoms
                   WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))
                     AND source_event_id IS NOT NULL)""",
            ids=dead_ids,
            now=now,
        )
        return {"df": len(desc), "dt": tombstoned_now, "vs": vs, "pd": pd, "ed": ed, "rs": rs}
