"""Hybrid lexical + vector ranking, strictly inside an authorized set.

The ranking queries join ``memory_projections`` to an explicit list of
``(version_id, view)`` pairs produced by the policy engine. They never scan
unauthorized rows, and they score each memory on the projection for the view
the requester holds, so hidden fields cannot influence rank.

Relevance bar
-------------
Sharing one word with the query is not enough. Each query term gets an IDF
weight computed over the requester's *authorized* projections only (so hidden
records cannot shift weights), and an item's lexical coverage is

    coverage = sum(idf of query terms it contains) / sum(idf of all query terms)

An item is relevant when its coverage clears ``min_coverage`` (and, with a real
sentence embedder, its similarity clears ``vector_support_similarity``), or when
its vector similarity alone clears ``vector_min_similarity``. Among relevant
items, those scoring below ``relative_cutoff`` x the best score are dropped, so
weak matches do not ride along with a strong one. Items with identical text are
collapsed (the best-scoring copy is kept).

Multi-part questions
--------------------
A question that asks for several things ("who is the vendor, which room, and
when is the demo?") dilutes every item's coverage. When the planner supplies
atomic sub-questions (``QueryPlan.parts``), the whole question and each part are
ranked separately, each with its own bar, and the lists are merged round-robin
up to ``top_k`` so every part gets a share of the evidence.

Score = w_lex * coverage + w_vec * max(cosine_similarity, 0). Vector similarity is
computed exactly (``<=>``) over the authorized subset, never through an
approximate index with a post-filter; see docs/RETRIEVAL.md. Ties break on
memory_id, so results are deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gsm.db.database import Tx, vector_literal
from gsm.disclosure.schemas import normalize
from gsm.domain.types import View
from gsm.providers.base import Embedder
from gsm.retrieval.planner import QueryPlan

_ALLOWED = """
    FROM memory_projections p
    JOIN unnest(CAST(:version_ids AS text[]), CAST(:views AS text[])) AS al(version_id, view)
      ON al.version_id = p.version_id AND al.view = p.view
    JOIN memory_versions v ON v.tenant_id = p.tenant_id AND v.version_id = p.version_id
    JOIN memory_atoms a ON a.tenant_id = p.tenant_id AND a.memory_id = p.memory_id
"""

# Document frequency of each query term within the authorized projections.
_DF_SQL = f"""
WITH proj AS (
    SELECT p.tsv {_ALLOWED}
    WHERE p.tenant_id = :tenant_id AND a.lifecycle_state = 'active' AND v.state <> 'tombstoned'
),
terms AS (
    SELECT t, to_tsquery('english', t) AS q FROM unnest(CAST(:terms AS text[])) AS t
)
SELECT terms.t AS term,
       numnode(terms.q) > 0 AS indexable,
       (SELECT count(*) FROM proj WHERE numnode(terms.q) > 0 AND proj.tsv @@ terms.q) AS df,
       (SELECT count(*) FROM proj) AS n
FROM terms
"""

_CANDIDATES_SQL = """
SELECT * FROM (
    SELECT p.memory_id, p.version_id, p.view, p.text, p.fields,
           a.semantic_type, a.topic, a.observed_at, a.is_derived, a.content_schema,
           v.valid_from, v.valid_until, v.state AS version_state, v.truth_state,
           ARRAY(SELECT t FROM unnest(CAST(:terms AS text[])) AS t
                 WHERE p.tsv @@ to_tsquery('english', t)) AS matched,
           {vec_expr} AS vec_sim
    {allowed}
    {vec_join}
    WHERE p.tenant_id = :tenant_id
      AND a.lifecycle_state = 'active'
      AND v.state <> 'tombstoned'
) s
WHERE cardinality(s.matched) > 0 OR COALESCE(s.vec_sim, -1) >= :min_sim
LIMIT :cap
"""

_VEC_JOIN = """LEFT JOIN embeddings e ON e.tenant_id = p.tenant_id AND e.version_id = p.version_id
                 AND e.view = p.view AND e.model = :emb_model"""
_VEC_EXPR = "CASE WHEN e.embedding IS NULL THEN NULL ELSE 1 - (e.embedding <=> CAST(:qvec AS vector)) END"


@dataclass(frozen=True)
class RankedRow:
    memory_id: str
    version_id: str
    view: View
    text: str
    fields: dict[str, str]
    semantic_type: str
    topic: str
    observed_at: datetime
    is_derived: bool
    content_schema: str
    valid_from: datetime
    valid_until: datetime | None
    version_state: str
    truth_state: str
    lexical_score: float  # IDF-weighted query coverage in [0, 1]
    lexical_hit: bool
    vector_similarity: float | None
    score: float


@dataclass(frozen=True)
class RankingConfig:
    top_k: int
    lexical_weight: float
    vector_weight: float
    vector_min_similarity: float
    min_coverage: float = 0.2
    relative_cutoff: float = 0.5
    dedup: bool = True
    candidate_cap: int = 500
    vector_support_similarity: float = -1.0


def term_weights(tx: Tx, pairs: Sequence[tuple[str, View]], terms: Sequence[str]) -> dict[str, float]:
    """IDF of each indexable query term over the authorized projections."""
    if not terms:
        return {}
    rows = tx.all(
        _DF_SQL.format(),
        version_ids=[p[0] for p in pairs],
        views=[p[1].value for p in pairs],
        terms=list(terms),
    )
    out: dict[str, float] = {}
    for r in rows:
        if not r["indexable"]:
            continue  # a stopword to the text-search dictionary: it can never match
        n, df = int(r["n"]), int(r["df"])
        out[r["term"]] = math.log((n + 1) / (df + 1)) + 1.0
    return out


# How far a candidate got for its best part; used to merge per-part notes.
_LEVEL = {"below_bar": 0, "relative_cut": 1, "ranked": 2}


def _rank_one(
    tx: Tx,
    pairs: Sequence[tuple[str, View]],
    plan: QueryPlan,
    cfg: RankingConfig,
    embedder: Embedder | None,
    qvec: list[float] | None,
) -> tuple[list[RankedRow], dict[str, dict[str, Any]]]:
    """Relevant rows for one (sub-)question, best first, after the relative cutoff."""
    weights = term_weights(tx, pairs, plan.tokens)
    total = sum(weights.values())
    params: dict[str, Any] = {
        "terms": list(weights),
        "version_ids": [p[0] for p in pairs],
        "views": [p[1].value for p in pairs],
        "min_sim": cfg.vector_min_similarity,
        "cap": cfg.candidate_cap,
    }
    if qvec is not None and embedder is not None:
        sql = _CANDIDATES_SQL.format(vec_expr=_VEC_EXPR, vec_join=_VEC_JOIN, allowed=_ALLOWED)
        params["qvec"] = vector_literal(qvec)
        params["emb_model"] = embedder.model_id
    else:
        sql = _CANDIDATES_SQL.format(vec_expr="CAST(NULL AS double precision)", vec_join="", allowed=_ALLOWED)
    wv = cfg.vector_weight if qvec is not None else 0.0
    scored: list[RankedRow] = []
    notes: dict[str, dict[str, Any]] = {}
    for r in tx.all(sql, **params):
        matched = list(r["matched"] or [])
        coverage = sum(weights.get(t, 0.0) for t in matched) / total if total else 0.0
        vec = float(r["vec_sim"]) if r["vec_sim"] is not None else None
        lexical_ok = coverage >= cfg.min_coverage and (vec is None or vec >= cfg.vector_support_similarity)
        vector_ok = vec is not None and vec >= cfg.vector_min_similarity
        notes[r["version_id"]] = {
            "memory_id": r["memory_id"],
            "view": r["view"],
            "matched": matched,
            "coverage": round(coverage, 4),
            "vec_sim": None if vec is None else round(vec, 4),
            "status": "below_bar",
        }
        if not (lexical_ok or vector_ok):
            continue
        score = cfg.lexical_weight * coverage + wv * max(vec or 0.0, 0.0)
        scored.append(
            RankedRow(
                memory_id=r["memory_id"],
                version_id=r["version_id"],
                view=View(r["view"]),
                text=r["text"],
                fields=dict(r["fields"] or {}),
                semantic_type=r["semantic_type"],
                topic=r["topic"],
                observed_at=r["observed_at"],
                is_derived=bool(r["is_derived"]),
                content_schema=r["content_schema"],
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
                version_state=r["version_state"],
                truth_state=r["truth_state"],
                lexical_score=round(coverage, 6),
                lexical_hit=bool(matched),
                vector_similarity=vec,
                score=round(score, 6),
            )
        )
    scored.sort(key=lambda x: (-x.score, x.memory_id))
    floor = cfg.relative_cutoff * scored[0].score if scored else 0.0
    out: list[RankedRow] = []
    for row in scored:
        note = notes[row.version_id]
        note["score"] = row.score
        note["status"] = "relative_cut" if row.score < floor else "ranked"
        if note["status"] == "ranked":
            out.append(row)
    return out, notes


def rank_within(
    tx: Tx,
    pairs: Sequence[tuple[str, View]],
    plan: QueryPlan,
    cfg: RankingConfig,
    embedder: Embedder | None,
    *,
    limit: int | None = None,
    explain: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[RankedRow]:
    """Rank the given authorized ``(version_id, view)`` pairs against the plan.

    ``explain`` (diagnostics only) receives every candidate that shared a term or
    cleared the vector threshold, with its coverage, similarity, score and fate:
    kept, below_bar, relative_cut, duplicate or beyond_top_k. With several parts,
    a candidate's note is the one from the part where it got furthest.
    """
    if not pairs or not plan.tokens:
        return []
    plans = plan.subplans()
    qvecs: list[list[float]] | None = None
    if embedder is not None:
        try:
            qvecs = embedder.embed([p.semantic_text or p.text for p in plans])
        except Exception:
            qvecs = None  # degrade to lexical-only
    lists: list[list[RankedRow]] = []
    notes: dict[str, dict[str, Any]] = {}
    for i, p in enumerate(plans):
        rows, part_notes = _rank_one(tx, pairs, p, cfg, embedder, qvecs[i] if qvecs else None)
        lists.append(rows)
        for vid, note in part_notes.items():
            note["part"] = i
            prev = notes.get(vid)
            if prev is None or (_LEVEL[note["status"]], note["coverage"]) > (_LEVEL[prev["status"]], prev["coverage"]):
                notes[vid] = note
    # Round-robin merge: the i-th best of every part before the (i+1)-th of any.
    k = limit or plan.top_k
    out: list[RankedRow] = []
    taken: set[str] = set()
    seen: set[str] = set()
    for rank in range(max((len(rows) for rows in lists), default=0)):
        for rows in lists:
            if rank >= len(rows) or rows[rank].version_id in taken:
                continue
            row = rows[rank]
            note = notes.get(row.version_id, {})
            key = normalize(row.text)
            if cfg.dedup and key in seen:
                note["status"] = "duplicate"
                continue
            if len(out) >= k:
                note["status"] = "beyond_top_k"
                continue
            taken.add(row.version_id)
            seen.add(key)
            out.append(row)
            note["status"] = "kept"
    if explain is not None:
        explain(list(notes.values()))
    return out


_ALL_SQL = f"""
SELECT p.memory_id, p.version_id, p.view, p.text, p.fields,
       a.semantic_type, a.topic, a.observed_at, a.is_derived, a.content_schema,
       v.valid_from, v.valid_until, v.state AS version_state, v.truth_state
{_ALLOWED}
WHERE p.tenant_id = :tenant_id AND a.lifecycle_state = 'active' AND v.state <> 'tombstoned'
ORDER BY a.observed_at, p.memory_id
LIMIT :cap
"""


def all_within(
    tx: Tx,
    pairs: Sequence[tuple[str, View]],
    *,
    max_items: int,
    max_chars: int,
    dedup: bool = True,
    explain: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[RankedRow] | None:
    """Every authorized projection, oldest first, when the whole set fits the budget.

    Returns None when it does not fit (the caller ranks instead). Used for answering
    when a requester's permitted memory is small: relevance filtering then only
    removes context the answerer needs (the facts asked about, and the records that
    show the requester's own standing). Nothing outside ``pairs`` is ever read.
    """
    if not pairs or len(pairs) > max_items:
        return None
    rows = tx.all(
        _ALL_SQL,
        version_ids=[p[0] for p in pairs],
        views=[p[1].value for p in pairs],
        cap=max_items + 1,
    )
    if len(rows) > max_items or sum(len(r["text"] or "") for r in rows) > max_chars:
        return None
    out: list[RankedRow] = []
    seen: set[str] = set()
    notes: list[dict[str, Any]] = []
    for r in rows:
        key = normalize(r["text"] or "")
        dup = dedup and key in seen
        notes.append(
            {
                "memory_id": r["memory_id"],
                "view": r["view"],
                "matched": [],
                "coverage": None,
                "vec_sim": None,
                "score": None,
                "status": "duplicate" if dup else "kept",
                "full_context": True,
            }
        )
        if dup:
            continue
        seen.add(key)
        out.append(
            RankedRow(
                memory_id=r["memory_id"],
                version_id=r["version_id"],
                view=View(r["view"]),
                text=r["text"],
                fields=dict(r["fields"] or {}),
                semantic_type=r["semantic_type"],
                topic=r["topic"],
                observed_at=r["observed_at"],
                is_derived=bool(r["is_derived"]),
                content_schema=r["content_schema"],
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
                version_state=r["version_state"],
                truth_state=r["truth_state"],
                lexical_score=0.0,
                lexical_hit=False,
                vector_similarity=None,
                score=0.0,
            )
        )
    if explain is not None:
        explain(notes)
    return out
