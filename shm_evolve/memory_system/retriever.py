"""Retriever: query -> candidates -> GATE -> selection.  (prompted; evolvable)

The scaffold fixes the steps; the prompt fills two of them.

1. `candidates()` runs a SearchSpec -- composed by the prompt ("search"
   stage) or by the fallback -- in SQL. The scaffold, never the model, adds
   `created_at <= as_of`, the tombstone filter and the current-version
   filter. Words are matched through the blind index (terms.py), scored by
   idf-weighted coverage of the query's terms, so candidates are found
   without opening a single body.
2. GATE. `keyring.open_for` with the ASKER's id, per candidate. Nothing else
   produces plaintext here; a candidate that does not open is a denial and
   its text never reaches the model. A principal restricted to a summary of
   a record gets the summary record, if one exists and opens for them.
3. `select()` -- the prompt ("select" stage) ranks the opened texts and picks
   the action; the fallback is DESIGN.md §2.5's rule.

`peek()` is the same search opened with the store's OWNER identity: the
processor's read-only view for maintenance. It never feeds `evidence`.

`answer()` performs no writes at all: its trace event is held by the tracer
until the next ingest (trace.py), so `query` leaves the database
byte-identical.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from contracts import ACTIONS, Component, EvidenceItem, Outcome, QueryResult, TraceEvent
from memory_system import directory as dirmod
from memory_system import envelope, keyring, prompting, terms
from memory_system.directory import Directory
from memory_system.ops import OpError, SearchSpec, parse_search, predicate_sql
from memory_system.trace import Tracer

#: Fallback relevance: a record must cover at least this share of the query's
#: idf weight AND match at least one query term that is not merely a declared
#: id's name. Sharing only "which thing" is being about something else; that
#: is what lets a question about deleted content come back no_memory instead
#: of being answered from a neighbour.
RELEVANCE_MIN = 0.2
FALLBACK_TOP_K = 8
SELECT_SHOWN = 12
PEEK_LIMIT = 6
TEXT_CAP_SELECT = 700
TEXT_CAP_PEEK = 320

_ROW_COLS = (
    "record_id",
    "collection",
    "body",
    "speaker_id",
    "speaker_role",
    "about",
    "created_at",
    "valid_to",
    "superseded_by",
    "derived_from",
    "granularity",
)


@dataclass
class Candidate:
    record_id: str
    collection: str
    body: bytes | None
    speaker_id: str
    speaker_role: str
    about: list[str]
    created_at: str
    valid_to: str | None
    superseded_by: str | None
    derived_from: list[str]
    granularity: str
    score: float = 0.0
    content_hits: int = 0  # matched query terms that are not declared-id names
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def current(self) -> bool:
        return self.valid_to is None

    def relevant(self, query_has_content: bool) -> bool:
        return self.score >= RELEVANCE_MIN and (self.content_hits > 0 or not query_has_content)


# ---------------------------------------------------------------- candidates


def _query_terms(spec: SearchSpec) -> set[str]:
    out = terms.terms(" ".join(spec.terms))
    out |= {a.lower() for a in spec.about}
    return out


def _scores(
    conn: sqlite3.Connection, qterms: set[str], id_terms: frozenset[str] | set[str] = frozenset()
) -> tuple[dict[str, float], dict[str, int]]:
    """record_id -> idf-weighted share of the query's terms the record holds,
    and record_id -> how many of its matches are content (non-id) terms."""
    if not qterms:
        return {}, {}
    key = envelope.index_key(conn)
    hashed = {terms.blind(key, t): t for t in qterms}
    marks = ",".join("?" * len(hashed))
    n_docs = conn.execute(
        "SELECT COUNT(*) FROM _record_index ri WHERE EXISTS "
        "(SELECT 1 FROM _terms t WHERE t.record_id = ri.record_id)"
    ).fetchone()[0]
    df = dict(
        conn.execute(
            f"SELECT term, COUNT(*) FROM _terms WHERE term IN ({marks}) GROUP BY term", list(hashed)
        ).fetchall()
    )
    weight = {h: terms.idf(n_docs, int(df.get(h, 0))) for h in hashed}
    total = sum(weight.values()) or 1.0
    scores: dict[str, float] = {}
    content: dict[str, int] = {}
    for rid, h in conn.execute(f"SELECT record_id, term FROM _terms WHERE term IN ({marks})", list(hashed)):
        scores[rid] = scores.get(rid, 0.0) + weight[h] / total
        content[rid] = content.get(rid, 0) + (hashed[h] not in id_terms)
    return scores, content


def candidates(
    conn: sqlite3.Connection,
    spec: SearchSpec,
    *,
    as_of: str | None,
    id_terms: frozenset[str] | set[str] = frozenset(),
) -> list[Candidate]:
    known = envelope.collections(conn)
    sources = [c for c in spec.collections if c in known or c in envelope.views(conn)] or known
    scores, content = _scores(conn, _query_terms(spec), id_terms)
    if spec.terms and not scores:
        return []
    # With words to match, the best-scoring ids are chosen first and the SQL
    # filters only those; without, the most recent rows that pass the filters.
    top_ids = sorted(scores, key=scores.__getitem__, reverse=True)[: max(spec.limit, 1) * 8]

    def fetch(strict: bool) -> list[Candidate]:
        out: list[Candidate] = []
        for src in sources:
            cols = envelope.columns(conn, src)
            where = ["tombstoned_at IS NULL"]
            params: list[Any] = []
            if as_of is not None:
                where.append("created_at <= ?")
                params.append(as_of)
            if spec.current_only:
                if as_of is None:
                    where.append("valid_to IS NULL")
                else:
                    where.append("(valid_to IS NULL OR valid_to > ?)")
                    params.append(as_of)
            if strict:
                usable = [c for c in spec.where if c.field in cols and c.field != "body"]
                if usable:
                    sql, p = predicate_sql(usable)
                    where.append(sql)
                    params += p
                if spec.about:
                    where.append("(" + " OR ".join("instr(about, ?) > 0" for _ in spec.about) + ")")
                    params += [json.dumps(a) for a in spec.about]
            tail = " ORDER BY created_at DESC LIMIT ?"
            if top_ids:
                where.append(f"record_id IN ({','.join('?' * len(top_ids))})")
                params += top_ids
                tail = ""
            else:
                params.append(max(spec.limit, 1) * 4)
            extra = [c for c in cols if c not in envelope.ENVELOPE_COLUMNS]
            select = ", ".join(f'"{c}"' for c in (*_ROW_COLS, *extra))
            rows = conn.execute(
                f'SELECT {select} FROM "{src}" WHERE {" AND ".join(where)}{tail}', params
            ).fetchall()
            for row in rows:
                d = dict(zip((*_ROW_COLS, *extra), row, strict=True))
                out.append(
                    Candidate(
                        record_id=d["record_id"],
                        collection=d["collection"],
                        body=d["body"],
                        speaker_id=d["speaker_id"],
                        speaker_role=d["speaker_role"],
                        about=json.loads(d["about"] or "[]"),
                        created_at=d["created_at"],
                        valid_to=d["valid_to"],
                        superseded_by=d["superseded_by"],
                        derived_from=json.loads(d["derived_from"] or "[]"),
                        granularity=d["granularity"],
                        score=round(scores.get(d["record_id"], 0.0), 4),
                        content_hits=content.get(d["record_id"], 0),
                        fields={k: d[k] for k in extra if d[k] is not None},
                    )
                )
        seen: set[str] = set()
        return [c for c in out if not (c.record_id in seen or seen.add(c.record_id))]

    # The model's `where` and `about` RANK, they do not exclude. Records that
    # satisfy them come first; when they leave the set short of the limit it
    # is topped up from the same word-matched pool without them. A filter
    # the model gets wrong -- `status = 'current'` on records that carry no
    # such value -- used to empty the candidate set and the query ended as
    # no_memory; on the first joint run that cost a third of the candidates.
    # Only the keyring gate excludes.
    filtered = bool(spec.where or spec.about)
    strict_set = fetch(True) if filtered else []
    unique = list(strict_set)
    if not filtered or len(unique) < spec.limit:
        have = {c.record_id for c in unique}
        unique += [c for c in fetch(False) if c.record_id not in have]
    strict_ids = {c.record_id for c in strict_set}
    unique.sort(
        key=lambda c: (c.record_id in strict_ids, c.current, c.content_hits > 0, c.score, c.created_at),
        reverse=True,
    )
    return unique[: spec.limit]


# ---------------------------------------------------------------- peek (OWNER, maintenance only)


def peek(
    conn: sqlite3.Connection,
    *,
    texts: list[str],
    about: list[str],
    extra_ids: list[str] = (),
    limit: int = PEEK_LIMIT,
) -> list[dict[str, Any]]:
    """What the processor sees of current memory: records sharing words or
    declared ids with the chunks, superseded versions included (a deletion
    must reach them too), opened with OWNER."""
    spec = SearchSpec(terms=list(texts), about=[], current_only=False, limit=limit)
    spec.terms += list(about)
    found = candidates(conn, spec, as_of=None)
    have = {c.record_id for c in found}
    for rid in extra_ids:
        rec = envelope.get_record(conn, rid)
        if envelope.is_live(rec) and rid not in have:
            found.append(
                Candidate(
                    record_id=rid,
                    collection=rec["collection"],
                    body=rec["body"],
                    speaker_id=rec["speaker_id"],
                    speaker_role=rec["speaker_role"],
                    about=rec["about"],
                    created_at=rec["created_at"],
                    valid_to=rec["valid_to"],
                    superseded_by=rec["superseded_by"],
                    derived_from=rec["derived_from"],
                    granularity=rec["granularity"],
                    fields=envelope.record_fields(rec),
                )
            )
    rows = []
    for c in found:
        text = keyring.open_for(conn, c.record_id, keyring.OWNER, c.body)
        if text is None:
            continue
        row: dict[str, Any] = {
            "record_id": c.record_id,
            "speaker": c.speaker_id,
            "about": c.about,
            "created_at": c.created_at,
            "current": c.current,
            "audience": envelope.audience(conn, c.record_id),
            "text": text[:TEXT_CAP_PEEK],
        }
        # Only what differs from the common case, to keep the processor's input short.
        optional = {
            "collection": c.collection if c.collection != envelope.DEFAULT_COLLECTION else None,
            "superseded_by": c.superseded_by,
            "derived_from": c.derived_from or None,
            "granularity": c.granularity if c.granularity != "full" else None,
            "fields": c.fields or None,
        }
        row |= {k: v for k, v in optional.items() if v is not None}
        rows.append(row)
    return rows


# ---------------------------------------------------------------- query


@dataclass
class _Opened:
    cand: Candidate
    text: str
    redacted: bool = False  # released at summary granularity for this asker


def _fallback_spec(query_text: str, mentioned: list[str]) -> SearchSpec:
    return SearchSpec(terms=[query_text], about=[], current_only=True, limit=40)


def _expired_for(conn: sqlite3.Connection, asker: str, as_of: str) -> set[str]:
    """Records whose grant to the asker expired by `as_of` but whose wrap has
    not been removed yet (removal happens at the next ingest). Read-only."""
    return {
        r[0]
        for r in conn.execute(
            "SELECT record_id FROM _expiries WHERE principal_id = ? AND applied = 0 AND at <= ?",
            (asker, as_of),
        )
    }


def _gate(
    conn: sqlite3.Connection, cands: list[Candidate], asker: str, as_of: str
) -> tuple[list[_Opened], list[Candidate], int]:
    expired = _expired_for(conn, asker, as_of)
    opened: list[_Opened] = []
    denied: list[Candidate] = []
    n_existence = 0
    for c in cands:
        text = None if c.record_id in expired else keyring.open_for(conn, c.record_id, asker, c.body)
        if text is not None:
            opened.append(_Opened(c, text))
            continue
        level = conn.execute(
            "SELECT granularity FROM _restrictions WHERE record_id = ? AND principal_id = ?",
            (c.record_id, asker),
        ).fetchone()
        if level and level[0] == "summary":
            summary = _summary_for(conn, c, asker)
            if summary is not None:
                opened.append(summary)
                continue
        if level:
            n_existence += 1
            continue
        denied.append(c)
    return opened, denied, n_existence


def _summary_for(conn: sqlite3.Connection, c: Candidate, asker: str) -> _Opened | None:
    for rid in envelope.derived_of(conn, c.record_id):
        rec = envelope.get_record(conn, rid)
        if not envelope.is_live(rec) or rec["granularity"] != "summary" or rec["valid_to"] is not None:
            continue
        text = keyring.open_for(conn, rid, asker, rec["body"])
        if text is not None:
            cand = Candidate(
                record_id=rid,
                collection=rec["collection"],
                body=rec["body"],
                speaker_id=rec["speaker_id"],
                speaker_role=rec["speaker_role"],
                about=rec["about"],
                created_at=rec["created_at"],
                valid_to=None,
                superseded_by=None,
                derived_from=rec["derived_from"],
                granularity="summary",
                score=c.score,
                content_hits=c.content_hits,
            )
            return _Opened(cand, text, redacted=True)
    return None


def _fallback_select(
    opened: list[_Opened], denied: list[Candidate], n_existence: int, has_content: bool
) -> tuple[list[_Opened], str]:
    relevant = [o for o in opened if o.cand.relevant(has_content)]
    relevant.sort(key=lambda o: (o.cand.current, o.cand.score, o.cand.created_at), reverse=True)
    chosen = relevant[:FALLBACK_TOP_K]
    if chosen:
        return chosen, ("answer_redacted" if all(o.redacted for o in chosen) else "answer")
    if n_existence:
        return [], "answer_redacted"
    if any(d.relevant(has_content) for d in denied):
        return [], "refuse"
    return [], "no_memory"


def _model_select(reply: Any, opened: list[_Opened]) -> tuple[list[_Opened], str] | None:
    if not isinstance(reply, dict) or reply.get("action") not in ACTIONS:
        return None
    by_id = {o.cand.record_id: o for o in opened}
    release = [
        by_id[r] for r in dict.fromkeys(reply.get("release") or []) if isinstance(r, str) and r in by_id
    ]
    action = reply["action"]
    if action in ("refuse", "no_memory"):
        return [], action
    if not release:
        return None
    return release, action


class Retriever:
    def __init__(
        self, conn: sqlite3.Connection, directory: Directory, tracer: Tracer, prompts_dir: str | None = None
    ) -> None:
        self.conn, self.directory, self.tracer, self.prompts_dir = conn, directory, tracer, prompts_dir

    def _search(
        self, query_text: str, asker: dict[str, str], mentioned: list[str]
    ) -> tuple[SearchSpec, bool, str]:
        payload = {
            "stage": "search",
            "query": query_text,
            "asker": asker,
            "mentioned": mentioned,
            "schema": envelope.schema_summary(self.conn),
            "policy_facts": dirmod.compact(
                dirmod.current_facts(self.conn, involving=[asker.get("principal_id", ""), *mentioned])
            ),
        }
        reply, called = prompting.ask(Component.RETRIEVER, payload, prompts_dir=self.prompts_dir)
        spec = _fallback_spec(query_text, mentioned)
        if isinstance(reply, dict):
            raw = reply.get("search", reply)
            note = ""
            try:
                model_spec = parse_search(raw)
            except OpError as exc:
                # A bad `where` clause used to void the whole spec, and with
                # it the `about` filter and the model's terms: on the first
                # live run 39% of searches degraded to the fallback this way
                # (a field the schema lacks, `eq` for `=`). The clause is the
                # broken part; the rest of the spec is kept.
                if not isinstance(raw, dict) or "where" not in raw:
                    return spec, called, f"search spec rejected: {exc}"
                try:
                    model_spec = parse_search({k: v for k, v in raw.items() if k != "where"})
                except OpError as exc2:
                    return spec, called, f"search spec rejected: {exc2}"
                note = f"where dropped: {exc}"
            # The query's own words always count: a model can add terms, it
            # cannot make the search forget what was asked.
            model_spec.terms = [query_text, *model_spec.terms]
            model_spec.about = [a for a in model_spec.about if self.directory.is_declared(a)]
            return model_spec, called, note
        return spec, called, "model unavailable" if not called else "unparseable search reply"

    def answer(self, checkpoint: dict[str, Any], as_of: str) -> QueryResult:
        asker = dict(checkpoint.get("asker") or {})
        asker_id = str(asker.get("principal_id") or "")
        query_text = str(checkpoint.get("query_text") or "")
        mentioned = self.directory.mentions(query_text)
        spec, search_by_model, search_note = self._search(query_text, asker, mentioned)
        id_terms = self.directory.id_terms()
        has_content = bool(_query_terms(spec) - id_terms)
        cands = candidates(self.conn, spec, as_of=as_of, id_terms=id_terms)
        opened, denied, n_existence = _gate(self.conn, cands, asker_id, as_of)

        shown = sorted(opened, key=lambda o: (o.cand.current, o.cand.score, o.cand.created_at), reverse=True)[
            :SELECT_SHOWN
        ]
        selection = None
        select_called = False
        if shown:
            payload = {
                "stage": "select",
                "query": query_text,
                "asker": asker,
                "opened": [
                    {
                        "record_id": o.cand.record_id,
                        "speaker": o.cand.speaker_id,
                        "speaker_role": o.cand.speaker_role,
                        "created_at": o.cand.created_at,
                        "current": o.cand.current,
                        "score": o.cand.score,
                        "matches_content": o.cand.content_hits > 0 or not has_content,
                        "granularity": "summary" if o.redacted else o.cand.granularity,
                        "text": o.text[:TEXT_CAP_SELECT],
                    }
                    for o in shown
                ],
                "n_denied": len(denied),
                "n_denied_relevant": sum(d.relevant(has_content) for d in denied),
                "n_existence_only": n_existence,
            }
            reply, select_called = prompting.ask(Component.RETRIEVER, payload, prompts_dir=self.prompts_dir)
            selection = _model_select(reply, shown) if reply is not None else None
        by_model = selection is not None
        if selection is None:
            selection = _fallback_select(opened, denied, n_existence, has_content)
        released, action = selection

        evidence = [
            EvidenceItem(
                record_id=o.cand.record_id,
                principal_id=o.cand.speaker_id,
                role=o.cand.speaker_role,
                text=o.text,
            )
            for o in released
        ]
        released_ids = [o.cand.record_id for o in released]
        denied_ids = [d.record_id for d in denied]
        cp_id = str(checkpoint.get("checkpoint_id") or "")
        turn_id = str(checkpoint.get("as_of_turn_id") or "")
        self.tracer.emit(
            TraceEvent(
                component=Component.RETRIEVER,
                turn_id=turn_id,
                checkpoint_id=cp_id,
                op="query",
                outcome=Outcome.APPLIED if by_model else Outcome.FALLBACK,
                llm_called=search_by_model or select_called,
                record_ids=released_ids,
                denied_ids=denied_ids,
                op_args={
                    "action": action,
                    "candidate_ids": [c.record_id for c in cands],
                    "terms": sorted(_query_terms(spec)),
                    "about": spec.about,
                    "collections": spec.collections,
                    "where": [[c.field, c.op, c.value] for c in spec.where],
                    "current_only": spec.current_only,
                    "n_opened": len(opened),
                    "n_existence_only": n_existence,
                    "redacted_ids": [o.cand.record_id for o in released if o.redacted],
                    "search_by_model": search_by_model and not search_note,
                    "select_by_model": by_model,
                },
                reason=search_note,
            ),
            defer=True,
        )
        if not evidence:
            self.tracer.emit(
                TraceEvent(
                    component=Component.RETRIEVER,
                    turn_id=turn_id,
                    checkpoint_id=cp_id,
                    op="query",
                    outcome=Outcome.LOGGED,
                    denied_ids=denied_ids,
                    signal_kind="read_time:empty",
                    op_args={"action": action},
                ),
                defer=True,
            )
        if denied:
            self.tracer.emit(
                TraceEvent(
                    component=Component.RETRIEVER,
                    turn_id=turn_id,
                    checkpoint_id=cp_id,
                    op="query",
                    outcome=Outcome.LOGGED,
                    denied_ids=denied_ids,
                    signal_kind="read_time:denied",
                    op_args={"n_denied": len(denied)},
                ),
                defer=True,
            )
        return QueryResult(
            action=action,
            evidence=evidence,
            used_record_ids=released_ids,
            debug={
                "retrieval": {
                    "n_allowed": len(opened),
                    "n_candidates": len(cands),
                    "n_denied": len(denied),
                    "n_released": len(evidence),
                    "n_existence_only": n_existence,
                    "n_redacted": sum(o.redacted for o in released),
                    "search_by_model": search_by_model and not search_note,
                    "select_by_model": by_model,
                }
            },
        )
