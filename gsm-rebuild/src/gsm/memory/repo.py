"""Tenant-scoped SQL used across the write, read and deletion paths.

Metadata queries (``ATOM_META_COLUMNS``) never select content columns, so the
policy engine can decide before any protected content leaves the database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gsm.db.database import Tx, jsonb, vector_literal
from gsm.disclosure.schemas import ContentSchema, project
from gsm.domain.content import Content
from gsm.domain.context import PrincipalContext
from gsm.domain.policy import parse_policy
from gsm.domain.types import DERIVATION_EDGES, LifecycleState, Sensitivity, VersionState, View
from gsm.ids import new_id
from gsm.policy.facts import AuthSnapshot, DenialFact, GrantFact, MemoryFacts, RelationFact, VersionFacts
from gsm.providers.base import Embedder

ATOM_META_COLUMNS = """
    a.tenant_id, a.workspace_id, a.memory_id, a.semantic_type, a.content_schema, a.lifecycle_state,
    a.retain_until, a.owner_ids, a.subject_ids, a.sensitivity, a.compartments, a.policy,
    a.policy_revision, a.allowed_views, a.is_derived, a.inherits_parent_policy, a.semantic_key,
    a.attribute, a.topic, a.source_event_id
"""

DERIVATION_EDGE_SQL = "('" + "','".join(sorted(e.value for e in DERIVATION_EDGES)) + "')"


def facts_from_row(row: Mapping[str, Any], parent_ids: Sequence[str] = ()) -> MemoryFacts:
    version = None
    if row.get("version_id"):
        version = VersionFacts(
            version_id=row["version_id"],
            state=VersionState(row["version_state"]),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
        )
    views: set[View] = set()
    for v in row["allowed_views"] or []:
        try:
            views.add(View(v))
        except ValueError:
            continue
    return MemoryFacts(
        memory_id=row["memory_id"],
        tenant_id=row["tenant_id"],
        workspace_id=row["workspace_id"],
        lifecycle_state=LifecycleState(row["lifecycle_state"]),
        retain_until=row["retain_until"],
        owner_ids=frozenset(row["owner_ids"] or []),
        subject_ids=frozenset(row["subject_ids"] or []),
        sensitivity=Sensitivity(row["sensitivity"]),
        compartments=frozenset(row["compartments"] or []),
        policy=parse_policy(row["policy"]),
        policy_revision=int(row["policy_revision"]),
        allowed_views=frozenset(views),
        is_derived=bool(row["is_derived"]),
        inherits_parent_policy=bool(row["inherits_parent_policy"]),
        parent_ids=tuple(parent_ids),
        version=version,
    )


def load_snapshot(tx: Tx, ctx: PrincipalContext) -> AuthSnapshot:
    if tx.tenant_id != ctx.tenant_id:
        raise PermissionError("transaction tenant does not match context tenant")
    now = ctx.request_time
    principal = tx.one(
        "SELECT active FROM principals WHERE tenant_id = :tenant_id AND principal_id = :pid",
        pid=ctx.principal_id,
    )
    rels = tx.all(
        """SELECT relation, object_type, object_id, valid_from, valid_until FROM relationships
           WHERE tenant_id = :tenant_id AND subject_principal_id = :pid AND removed_at IS NULL
             AND valid_from <= :now AND (valid_until IS NULL OR valid_until > :now)""",
        pid=ctx.principal_id,
        now=now,
    )
    grants = tx.all(
        """SELECT grant_id, memory_id, compartment, view, purposes, valid_from, expires_at, revoked_at, granted_by
           FROM explicit_grants
           WHERE tenant_id = :tenant_id AND grantee_principal_id = :pid AND revoked_at IS NULL
             AND valid_from <= :now AND (expires_at IS NULL OR expires_at > :now)""",
        pid=ctx.principal_id,
        now=now,
    )
    denials = tx.all(
        """SELECT denial_id, resource_type, resource_id, valid_from, valid_until, lifted_at
           FROM explicit_denials
           WHERE tenant_id = :tenant_id AND principal_id = :pid AND lifted_at IS NULL
             AND valid_from <= :now AND (valid_until IS NULL OR valid_until > :now)""",
        pid=ctx.principal_id,
        now=now,
    )
    memory_grants: dict[str, list[GrantFact]] = {}
    compartment_grants: list[GrantFact] = []
    for g in grants:
        fact = GrantFact(
            grant_id=g["grant_id"],
            memory_id=g["memory_id"],
            compartment=g["compartment"],
            view=View(g["view"]) if g["view"] else None,
            purposes=frozenset(g["purposes"] or []),
            valid_from=g["valid_from"],
            expires_at=g["expires_at"],
            revoked_at=g["revoked_at"],
            granted_by=g["granted_by"],
        )
        if fact.memory_id:
            memory_grants.setdefault(fact.memory_id, []).append(fact)
        elif fact.compartment:
            compartment_grants.append(fact)
    return AuthSnapshot(
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        principal_active=bool(principal and principal["active"]),
        taken_at=now,
        relations=tuple(
            RelationFact(r["relation"], r["object_type"], r["object_id"], r["valid_from"], r["valid_until"])
            for r in rels
        ),
        memory_grants={k: tuple(v) for k, v in memory_grants.items()},
        compartment_grants=tuple(compartment_grants),
        denials=tuple(
            DenialFact(
                d["denial_id"], d["resource_type"], d["resource_id"], d["valid_from"], d["valid_until"], d["lifted_at"]
            )
            for d in denials
        ),
    )


def resolve_roles(tx: Tx, principal_id: str, now: datetime) -> frozenset[str]:
    rows = tx.all(
        """SELECT role_name FROM principal_roles
           WHERE tenant_id = :tenant_id AND principal_id = :pid AND revoked_at IS NULL
             AND valid_from <= :now AND (valid_until IS NULL OR valid_until > :now)""",
        pid=principal_id,
        now=now,
    )
    return frozenset(r["role_name"] for r in rows)


def parent_map(tx: Tx, memory_ids: Iterable[str]) -> dict[str, list[str]]:
    ids = sorted(set(memory_ids))
    if not ids:
        return {}
    rows = tx.all(
        f"""SELECT dst_memory_id, src_memory_id FROM memory_edges
            WHERE tenant_id = :tenant_id AND dst_memory_id = ANY(CAST(:ids AS text[]))
              AND edge_type IN {DERIVATION_EDGE_SQL}
            ORDER BY src_memory_id""",
        ids=ids,
    )
    out: dict[str, list[str]] = {i: [] for i in ids}
    for r in rows:
        out[r["dst_memory_id"]].append(r["src_memory_id"])
    return out


def ancestors(tx: Tx, memory_ids: Iterable[str]) -> set[str]:
    ids = sorted(set(memory_ids))
    if not ids:
        return set()
    rows = tx.all(
        f"""WITH RECURSIVE up(mid) AS (
                SELECT src_memory_id FROM memory_edges
                WHERE tenant_id = :tenant_id AND dst_memory_id = ANY(CAST(:ids AS text[]))
                  AND edge_type IN {DERIVATION_EDGE_SQL}
                UNION
                SELECT e.src_memory_id FROM memory_edges e JOIN up ON e.dst_memory_id = up.mid
                WHERE e.tenant_id = :tenant_id AND e.edge_type IN {DERIVATION_EDGE_SQL}
            ) SELECT mid FROM up""",
        ids=ids,
    )
    return {r["mid"] for r in rows}


def descendants(tx: Tx, memory_ids: Iterable[str]) -> list[tuple[str, int]]:
    """All derivation descendants with their depth (1 = direct child)."""
    ids = sorted(set(memory_ids))
    if not ids:
        return []
    rows = tx.all(
        f"""WITH RECURSIVE down(mid, depth) AS (
                SELECT dst_memory_id, 1 FROM memory_edges
                WHERE tenant_id = :tenant_id AND src_memory_id = ANY(CAST(:ids AS text[]))
                  AND edge_type IN {DERIVATION_EDGE_SQL}
                UNION
                SELECT e.dst_memory_id, down.depth + 1 FROM memory_edges e JOIN down ON e.src_memory_id = down.mid
                WHERE e.tenant_id = :tenant_id AND e.edge_type IN {DERIVATION_EDGE_SQL} AND down.depth < 32
            ) SELECT mid, max(depth) AS depth FROM down GROUP BY mid ORDER BY max(depth), mid""",
        ids=ids,
    )
    return [(r["mid"], int(r["depth"])) for r in rows]


def load_atom_meta(tx: Tx, memory_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None:
    lock = " FOR UPDATE OF a" if for_update else ""
    return tx.one(
        f"""SELECT {ATOM_META_COLUMNS}, a.superseded_by_memory_id
            FROM memory_atoms a WHERE a.tenant_id = :tenant_id AND a.memory_id = :mid{lock}""",
        mid=memory_id,
    )


def current_version(tx: Tx, memory_id: str, now: datetime) -> Mapping[str, Any] | None:
    return tx.one(
        """SELECT version_id, state AS version_state, valid_from, valid_until, semantic_key
           FROM memory_versions
           WHERE tenant_id = :tenant_id AND memory_id = :mid AND state = 'active'
             AND valid_from <= :now AND (valid_until IS NULL OR valid_until > :now)""",
        mid=memory_id,
        now=now,
    )


def latest_version(tx: Tx, memory_id: str) -> Mapping[str, Any] | None:
    return tx.one(
        """SELECT version_id, state AS version_state, valid_from, valid_until, semantic_key
           FROM memory_versions
           WHERE tenant_id = :tenant_id AND memory_id = :mid AND state <> 'tombstoned'
           ORDER BY valid_from DESC, created_at DESC LIMIT 1""",
        mid=memory_id,
    )


def load_content(tx: Tx, version_id: str) -> Content | None:
    """Full content for trusted use only (projection building, release fingerprints)."""
    row = tx.one(
        """SELECT content_text, content_fields FROM memory_versions
           WHERE tenant_id = :tenant_id AND version_id = :vid AND state <> 'tombstoned'""",
        vid=version_id,
    )
    if row is None or (row["content_text"] is None and row["content_fields"] is None):
        return None
    return Content(text=row["content_text"], fields=row["content_fields"] or {})


@dataclass(frozen=True)
class IndexRow:
    view: View
    fields: dict[str, str]
    text: str
    vector: list[float] | None


def build_index_rows(
    schema: ContentSchema,
    content: Content,
    *,
    topic: str,
    semantic_type: str,
    allowed_views: Iterable[View],
    embedder: Embedder | None,
) -> list[IndexRow]:
    """One projection per disclosure view, plus the existence-only label that
    refusal ranking uses. Embeddings are computed per projection, so vector
    similarity can never reflect a field the requester's view hides."""
    views = sorted(set(allowed_views) | {View.EXISTENCE_ONLY}, key=lambda v: -v.rank)
    projections = [project(schema, v, content, topic=topic, semantic_type=semantic_type) for v in views]
    vectors: list[list[float]] | list[None] = [None] * len(projections)
    if embedder is not None:
        try:
            vectors = embedder.embed([p.text for p in projections])
        except Exception:
            # Safe failure: lexical-only for this version; reindex can fill later.
            vectors = [None] * len(projections)
    return [IndexRow(p.view, p.fields, p.text, vec) for p, vec in zip(projections, vectors, strict=True)]


def insert_index_rows(
    tx: Tx, *, memory_id: str, version_id: str, rows: Sequence[IndexRow], embedder: Embedder | None, now: datetime
) -> None:
    for r in rows:
        tx.exec(
            """INSERT INTO memory_projections (tenant_id, memory_id, version_id, view, fields, text, created_at)
               VALUES (:tenant_id, :mid, :vid, :view, CAST(:fields AS jsonb), :text, :now)""",
            mid=memory_id,
            vid=version_id,
            view=r.view.value,
            fields=jsonb(r.fields),
            text=r.text,
            now=now,
        )
        if r.vector is not None and embedder is not None:
            tx.exec(
                """INSERT INTO embeddings (tenant_id, embedding_id, memory_id, version_id, view, model, dim,
                                           embedding, created_at)
                   VALUES (:tenant_id, :eid, :mid, :vid, :view, :model, :dim, CAST(:vec AS vector), :now)""",
                eid=new_id("emb"),
                mid=memory_id,
                vid=version_id,
                view=r.view.value,
                model=embedder.model_id,
                dim=len(r.vector),
                vec=vector_literal(r.vector),
                now=now,
            )
