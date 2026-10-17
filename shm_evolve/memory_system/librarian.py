"""Librarian: applies ops to the store.  (prompted for structure only)

Record and policy ops are deterministic: their meaning is fixed by the
vocabulary, and what remains to decide (which records, which principals) was
decided by the processor. The prompt is consulted only for STRUCTURE ops,
where it picks the concrete DDL -- column name and type, which collection,
index columns, view predicate -- within the closed vocabulary; envelope.py
writes the statement. With no model the processor's proposal runs as is.

What the scaffold enforces here, whatever either prompt says:

  * Widening is bounded. `add`/`split`/`supersede` can give a record at most
    the chunk's suggested audience (plus the speaker). Anything wider needs a
    `grant`, and a grant needs an `authority` in the source chunk's
    about ∪ {speaker} -- and that authority must itself be able to see the
    record: nobody consents to sharing what they do not hold.
  * Tightening wins ties. Every revoke/restrict is written to `_ledger`; a
    later-applied widening that comes from a chunk no newer than the
    tightening (it waited in the queue while the tightening took the fast
    path) is refused for the principals it names. DESIGN.md §2.1 says order
    does not matter for tightening; it does for the widening queued behind
    it, and this is the guard.
  * Derived records' audiences are the intersection of their sources', and
    shrink when a source's does.

After each pass it computes the integrity signals the processor sees next
time (ops.IntegrityKind), from what this pass touched only -- each pass is
proportional to the batch, not to the store.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from contracts import Component, Outcome, TraceEvent
from memory_system import directory as dirmod
from memory_system import envelope, keyring, prompting
from memory_system.directory import Directory
from memory_system.ops import (
    GRANULARITIES,
    Chunk,
    IntegrityKind,
    IntegritySignal,
    Op,
    OpError,
    StructureOp,
    parse_ops,
    validate_op,
)
from memory_system.processor import Planned
from memory_system.trace import Tracer

#: At most this many signals of one kind go to the next processor call.
SIGNALS_PER_KIND = 4

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def _norm_body(text: str) -> str:
    return hashlib.sha256(_SPACE.sub(" ", _PUNCT.sub(" ", text.lower())).strip().encode()).hexdigest()


def _norm_span(text: str) -> str:
    return _SPACE.sub(" ", (text or "").lower()).strip(" \t\n\"'`.,;:!?()[]{}-")


class Rejected(OpError):
    """An op the scaffold refuses at apply time."""


class Librarian:
    def __init__(
        self, conn: sqlite3.Connection, directory: Directory, tracer: Tracer, prompts_dir: str | None = None
    ) -> None:
        self.conn, self.directory, self.tracer, self.prompts_dir = conn, directory, tracer, prompts_dir
        # record_id -> normalised-body hash of CURRENT records, in memory only:
        # a body hash on disk would outlive a shred.
        self._current_hash: dict[str, str] = {}
        self._reported_links: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ pass

    def apply(self, planned: list[Planned], chunks: list[Chunk]) -> list[IntegritySignal]:
        by_id = {c.chunk_id: c for c in chunks}
        batch_at = max((c.timestamp for c in chunks), default="")
        self._new: list[str] = []
        self._rekeyed: list[str] = []
        self._gone: list[str] = []
        self._mismatch: dict[tuple[str, str], dict[str, Any]] = {}
        # One model consultation for every structure op in the batch. Per-op
        # calls were a third of all endpoint time on the first joint run
        # (~80 reasoning calls per episode at 14 s); a batch is one call.
        structure_ops = [p.op for p in planned if p.op.group == "structure"]
        decisions = self._refine_batch(structure_ops) if structure_ops else []
        decided = iter(decisions)
        for p in planned:
            chunk = by_id.get(p.op.chunk_id)
            at = chunk.timestamp if chunk else batch_at
            structure = p.op.group == "structure"
            llm_called = p.llm_called
            try:
                if structure:
                    op, refined_by_model, called, decline = next(decided)
                    llm_called = llm_called or called
                    if decline:
                        raise Rejected(f"librarian declined: {decline[:80]}")
                    record_ids, note = self._structure(op, at)
                    args = op.args | {
                        "proposed_by": "fallback" if p.fallback else "processor",
                        "refined_by_model": refined_by_model,
                    }
                else:
                    record_ids, note = self._dispatch(p.op, chunk, at)
                    args = p.op.args
                outcome = Outcome.FALLBACK if p.fallback else Outcome.APPLIED
            except (OpError, sqlite3.Error) as exc:
                record_ids, note, args = [], str(exc), p.op.args
                outcome = Outcome.REJECTED
            self.tracer.emit(
                TraceEvent(
                    component=Component.LIBRARIAN if structure else Component.PROCESSOR,
                    turn_id=chunk.turn_id if chunk else (chunks[-1].turn_id if chunks else ""),
                    chunk_id=p.op.chunk_id,
                    op=p.op.name,
                    outcome=outcome,
                    op_args=args,
                    record_ids=record_ids,
                    reason="; ".join(x for x in (p.reason, note) if x),
                    llm_called=llm_called,
                    signal_kind=p.op.signal_kind,
                )
            )
        signals = self._signals()
        for s in signals:
            self.tracer.emit(
                TraceEvent(
                    component=Component.LIBRARIAN,
                    turn_id=chunks[-1].turn_id if chunks else "",
                    op="signal",
                    outcome=Outcome.LOGGED,
                    record_ids=s.record_ids,
                    op_args=s.detail,
                    signal_kind=f"integrity:{s.kind}",
                )
            )
        return signals

    def _dispatch(self, op: Op, chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        handler = getattr(self, f"_op_{op.name}", None)
        if handler is None:
            raise Rejected(f"no handler for {op.name}")
        return handler(op.args, chunk, at)

    # ------------------------------------------------------------ audience

    def _ledger_blocks(self, principal: str, *, since: str, about: list[str], record_id: str | None) -> bool:
        """True if a tightening at or after `since` covers this principal for
        a record with this `about` (or this exact record)."""
        for rid, scope, _at in self.conn.execute(
            "SELECT record_id, scope, at FROM _ledger WHERE principal_id = ? AND at >= ?", (principal, since)
        ):
            if record_id is not None and rid == record_id:
                return True
            if set(json.loads(scope)) & set(about):
                return True
        return False

    def _bounded_audience(self, requested: Any, chunk: Chunk, about: list[str]) -> tuple[set[str], str]:
        allowed = set(chunk.suggested_audience) | {chunk.speaker_id}
        if isinstance(requested, list):
            aud = ({a for a in requested if isinstance(a, str)} & allowed) | {chunk.speaker_id}
            dropped = sorted({a for a in requested if isinstance(a, str)} - allowed)
        else:
            aud, dropped = allowed, []
        blocked = sorted(
            p
            for p in aud
            if p != chunk.speaker_id
            and self._ledger_blocks(p, since=chunk.timestamp, about=about, record_id=None)
        )
        aud -= set(blocked)
        notes = []
        if dropped:
            notes.append(f"audience beyond the suggested one needs a grant: dropped {dropped}")
        if blocked:
            notes.append(f"a later tightening covers {blocked}")
        return aud, "; ".join(notes)

    def _about(self, value: Any, chunk: Chunk) -> list[str]:
        if isinstance(value, list):
            return [a for a in value if isinstance(a, str) and self.directory.is_declared(a)]
        return list(chunk.about)

    def _insert(
        self,
        *,
        text: str,
        chunk: Chunk,
        about: list[str],
        audience: set[str],
        collection: str,
        granularity: str = "full",
        fields: dict[str, Any] | None = None,
        derived_from: list[str] = (),
    ) -> str:
        if collection not in envelope.collections(self.conn):
            raise Rejected(f"no collection {collection!r}")
        if granularity not in GRANULARITIES:
            raise Rejected(f"granularity {granularity!r}")
        rid, unmatched = envelope.insert_record(
            self.conn,
            text=text,
            speaker_id=chunk.speaker_id,
            speaker_role=chunk.speaker_role,
            about=about,
            source_turn=chunk.turn_id,
            created_at=chunk.timestamp,
            audience=audience,
            collection=collection,
            granularity=granularity,
            derived_from=derived_from,
            field_values=fields,
        )
        for k, v in unmatched.items():
            self._mismatch.setdefault(
                (collection, k), {"collection": collection, "field": k, "value": v, "record_id": rid}
            )
        self._current_hash[rid] = _norm_body(text)
        self._new.append(rid)
        return rid

    def _live(self, rid: Any, *, current: bool = False) -> dict[str, Any]:
        rec = envelope.get_record(self.conn, rid) if isinstance(rid, str) else None
        if not envelope.is_live(rec):
            raise Rejected(f"record {rid!r} is unknown or deleted")
        if current and rec["valid_to"] is not None:
            raise Rejected(f"record {rid!r} is not the current version")
        return rec

    @staticmethod
    def _require_chunk(chunk: Chunk | None, name: str) -> Chunk:
        if chunk is None:
            raise Rejected(f"{name} needs a source chunk of this batch")
        return chunk

    # ------------------------------------------------------------ record ops

    def _op_add(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        chunk = self._require_chunk(chunk, "add")
        about = self._about(a.get("about"), chunk)
        aud, note = self._bounded_audience(a.get("audience"), chunk, about)
        fields = dict(chunk.attributes) | (a.get("fields") if isinstance(a.get("fields"), dict) else {})
        rid = self._insert(
            text=chunk.text,
            chunk=chunk,
            about=about,
            audience=aud,
            collection=a.get("collection") or envelope.DEFAULT_COLLECTION,
            granularity=a.get("granularity") or "full",
            fields=fields,
        )
        return [rid], note

    def _op_split(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        chunk = self._require_chunk(chunk, "split")
        parts = a.get("parts")
        if not isinstance(parts, list) or not parts:
            raise Rejected("split: parts must be a non-empty list")
        whole = _norm_span(chunk.text)
        for p in parts:
            if (
                not isinstance(p, dict)
                or not str(p.get("text") or "").strip()
                or _norm_span(str(p["text"])) not in whole
            ):
                raise Rejected("split: every part must be a verbatim span of the chunk")
        out, notes = [], []
        for p in parts:
            about = self._about(p.get("about"), chunk)
            aud, note = self._bounded_audience(p.get("audience"), chunk, about)
            notes.append(note)
            out.append(
                self._insert(
                    text=str(p["text"]).strip(),
                    chunk=chunk,
                    about=about,
                    audience=aud,
                    collection=a.get("collection") or envelope.DEFAULT_COLLECTION,
                    fields=dict(chunk.attributes),
                )
            )
        return out, "; ".join(n for n in notes if n)

    def _op_supersede(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        chunk = self._require_chunk(chunk, "supersede")
        old = self._live(a.get("old_record_id"), current=True)
        about = self._about(a.get("about"), chunk) or list(old["about"])
        aud, note = self._bounded_audience(a.get("audience"), chunk, about)
        # The same author (or the one it is about) restating a value keeps the
        # distribution the old value had; anyone else's correction does not
        # inherit an audience they were never given.
        inherited = []
        if chunk.speaker_id == old["speaker_id"] or chunk.speaker_id in old["about"]:
            for p in envelope.audience(self.conn, old["record_id"]):
                if p not in aud and not self._ledger_blocks(
                    p, since=chunk.timestamp, about=about, record_id=old["record_id"]
                ):
                    aud.add(p)
                    inherited.append(p)
        fields = (
            envelope.record_fields(old)
            | dict(chunk.attributes)
            | (a.get("fields") if isinstance(a.get("fields"), dict) else {})
        )
        rid = self._insert(
            text=chunk.text,
            chunk=chunk,
            about=about,
            audience=aud,
            collection=a.get("collection") or old["collection"],
            fields=fields,
        )
        envelope.supersede(self.conn, old["record_id"], rid, chunk.timestamp)
        self._current_hash.pop(old["record_id"], None)
        if inherited:
            note = "; ".join(x for x in (note, f"inherited audience {sorted(inherited)}") if x)
        return [rid, old["record_id"]], note

    def _sources(self, ids: Any, minimum: int) -> list[dict[str, Any]]:
        if not isinstance(ids, list) or len(set(ids)) < minimum:
            raise Rejected(f"needs at least {minimum} distinct source records")
        return [self._live(r) for r in dict.fromkeys(ids)]

    def _synthetic_chunk(self, sources: list[dict[str, Any]], chunk: Chunk | None, at: str) -> Chunk:
        latest = max(sources, key=lambda r: r["created_at"])
        return Chunk(
            chunk_id=chunk.chunk_id if chunk else "",
            turn_id=chunk.turn_id if chunk else latest["source_turn"],
            timestamp=at or latest["created_at"],
            speaker_id=latest["speaker_id"],
            speaker_role=latest["speaker_role"],
            text="",
            kind="fact",
        )

    def _op_merge(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        sources = self._sources(a.get("record_ids"), 2)
        text = str(a.get("text") or "").strip()
        if not text:
            raise Rejected("merge: empty text")
        ids = [s["record_id"] for s in sources]
        aud = set.intersection(*(set(envelope.audience(self.conn, r)) for r in ids))
        about = sorted({x for s in sources for x in s["about"]})
        rid = self._insert(
            text=text,
            chunk=self._synthetic_chunk(sources, chunk, at),
            about=about,
            audience=aud,
            collection=sources[0]["collection"],
            derived_from=ids,
        )
        for s in sources:
            if s["valid_to"] is None:
                envelope.supersede(self.conn, s["record_id"], rid, at)
                self._current_hash.pop(s["record_id"], None)
        return [rid, *ids], ""

    def _op_derive(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        sources = self._sources(a.get("source_record_ids"), 1)
        text = str(a.get("text") or "").strip()
        if not text:
            raise Rejected("derive: empty text")
        ids = [s["record_id"] for s in sources]
        aud = envelope.derived_audience(self.conn, ids)
        about = sorted({x for s in sources for x in s["about"]})
        rid = self._insert(
            text=text,
            chunk=self._synthetic_chunk(sources, chunk, at),
            about=about,
            audience=aud,
            collection=sources[0]["collection"],
            granularity=a.get("granularity") or "summary",
            derived_from=ids,
        )
        return [rid, *ids], ""

    def _op_link(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        r1, r2 = self._live(a.get("record_id")), self._live(a.get("other_record_id"))
        relation = str(a.get("relation") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", relation):
            raise Rejected("link: relation must be an identifier")
        self.conn.execute(
            "INSERT INTO _links(record_id, other_record_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (r1["record_id"], r2["record_id"], relation, at),
        )
        return [r1["record_id"], r2["record_id"]], ""

    def _remove(self, a: dict[str, Any], at: str, *, shred: bool) -> tuple[list[str], str]:
        done, missing = [], []
        for rid in dict.fromkeys(a.get("record_ids") or []):
            if envelope.is_live(envelope.get_record(self.conn, rid)):
                envelope.tombstone(self.conn, rid, at, null_body=shred)
                self._current_hash.pop(rid, None)
                self._gone.append(rid)
                done.append(rid)
            else:
                missing.append(rid)
        if not done and missing:
            raise Rejected(f"no live record among {len(missing)} named")
        return done, f"{len(missing)} named records were already gone" if missing else ""

    def _op_delete(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        return self._remove(a, at, shred=False)

    def _op_shred(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        return self._remove(a, at, shred=True)

    # ------------------------------------------------------------ policy ops

    def _principals(self, a: dict[str, Any]) -> list[str]:
        ps = [p for p in dict.fromkeys(a.get("principal_ids") or []) if p != keyring.OWNER]
        known = [p for p in ps if self.directory.is_principal(p)]
        if not known:
            raise Rejected("no declared principal named")
        return known

    def _scope(self, a: dict[str, Any]) -> list[str]:
        scope = a.get("scope") or []
        if not isinstance(scope, list):
            raise Rejected("scope must be a list of declared ids")
        return [s for s in scope if isinstance(s, str) and self.directory.is_declared(s)]

    def _targets(self, a: dict[str, Any], scope: list[str]) -> list[dict[str, Any]]:
        ids = list(
            dict.fromkeys(
                [
                    *(a.get("record_ids") or []),
                    *(envelope.live_ids(self.conn, about_any=scope, current_only=False) if scope else []),
                ]
            )
        )
        recs = [envelope.get_record(self.conn, r) for r in ids]
        return [r for r in recs if envelope.is_live(r)]

    def _op_grant(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        chunk = self._require_chunk(chunk, "grant")
        authority = a.get("authority")
        if authority not in set(chunk.about) | {chunk.speaker_id}:
            raise Rejected(f"authority {authority!r} is neither the speaker nor someone the chunk is about")
        principals = self._principals(a)
        scope = self._scope(a)
        granted, skipped = [], []
        for rid in dict.fromkeys(a.get("record_ids") or []):
            rec = envelope.get_record(self.conn, rid)
            if not envelope.is_live(rec):
                skipped.append(f"{rid}: gone")
                continue
            if (
                authority not in envelope.audience(self.conn, rid)
                and authority != rec["speaker_id"]
                and authority not in rec["about"]
            ):
                skipped.append(f"{rid}: authority cannot see it")
                continue
            for p in principals:
                if self._ledger_blocks(p, since=chunk.timestamp, about=rec["about"], record_id=rid):
                    skipped.append(f"{rid}->{p}: a later tightening covers it")
                    continue
                if keyring.grant(self.conn, rid, p):
                    self.conn.execute(
                        "DELETE FROM _restrictions WHERE record_id = ? AND principal_id = ?", (rid, p)
                    )
                    if rid not in granted:
                        granted.append(rid)
        facts = []
        if scope:
            if authority not in dirmod.group(self.conn, scope) | set(scope):
                skipped.append("scope: authority holds no current fact over it")
            else:
                for p in principals:
                    if not self._ledger_blocks(p, since=chunk.timestamp, about=scope, record_id=None):
                        facts.append(
                            dirmod.add_fact(
                                self.conn,
                                links=[p, *scope],
                                holders=[p],
                                attributes={"granted_by": authority},
                                qualified=False,
                                source=chunk.turn_id,
                                at=at,
                            )
                        )
        if not granted and not facts:
            raise Rejected(
                "grant changed nothing: " + "; ".join(skipped[:6]) if skipped else "grant named no records"
            )
        note = "; ".join(skipped[:6])
        if facts:
            note = "; ".join(x for x in (note, f"facts {facts}") if x)
        return granted, note

    def withdraw(self, rid: str, p: str) -> None:
        keyring.revoke(self.conn, rid, p)
        for d in envelope.derived_of(self.conn, rid):
            keyring.revoke(self.conn, d, p)

    def _op_revoke(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        principals, scope = self._principals(a), self._scope(a)
        touched = []
        for rec in self._targets(a, scope):
            for p in principals:
                self.withdraw(rec["record_id"], p)
                self.conn.execute(
                    "DELETE FROM _restrictions WHERE record_id = ? AND principal_id = ?",
                    (rec["record_id"], p),
                )
                self.conn.execute(
                    "INSERT INTO _ledger(principal_id, record_id, scope, kind, at) VALUES (?, ?, ?, ?, ?)",
                    (p, rec["record_id"], "[]", "revoke", at),
                )
            touched.append(rec["record_id"])
        ended = []
        for p in principals:
            if scope:
                ended += dirmod.end_facts(self.conn, p, scope, at)
                self.conn.execute(
                    "INSERT INTO _ledger(principal_id, record_id, scope, kind, at) VALUES (?, NULL, ?, ?, ?)",
                    (p, json.dumps(scope), "revoke", at),
                )
        return touched, f"ended facts {ended}" if ended else ""

    def _op_restrict(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        level = a.get("granularity")
        if level == "full":
            raise Rejected("restrict to full is not a narrowing; use grant")
        principals, scope = self._principals(a), self._scope(a)
        touched = []
        for rec in self._targets(a, scope):
            rid = rec["record_id"]
            for p in principals:
                held = p in envelope.audience(self.conn, rid)
                self.withdraw(rid, p)
                self.conn.execute(
                    "INSERT OR REPLACE INTO _restrictions(record_id, principal_id, granularity, at) "
                    "VALUES (?, ?, ?, ?)",
                    (rid, p, level, at),
                )
                self.conn.execute(
                    "INSERT INTO _ledger(principal_id, record_id, scope, kind, at) VALUES (?, ?, ?, ?, ?)",
                    (p, rid, "[]", "restrict", at),
                )
                # A summary is a narrowing of what they held, not a widening.
                if held and level == "summary":
                    for d in envelope.derived_of(self.conn, rid):
                        drec = envelope.get_record(self.conn, d)
                        if drec and drec["granularity"] == "summary":
                            keyring.grant(self.conn, d, p)
            touched.append(rid)
        for p in principals:
            if scope:
                self.conn.execute(
                    "INSERT INTO _ledger(principal_id, record_id, scope, kind, at) VALUES (?, NULL, ?, ?, ?)",
                    (p, json.dumps(scope), "restrict", at),
                )
        return touched, ""

    def _op_expire(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        when = a.get("at")
        if not isinstance(when, str) or not when.strip():
            raise Rejected("expire: `at` must be a timestamp string")
        principals = self._principals(a)
        touched = []
        for rec in self._targets(a, []):
            for p in principals:
                due = when <= at
                if due:
                    self.withdraw(rec["record_id"], p)
                self.conn.execute(
                    "INSERT INTO _expiries(record_id, principal_id, at, applied) VALUES (?, ?, ?, ?)",
                    (rec["record_id"], p, when, int(due)),
                )
            touched.append(rec["record_id"])
        return touched, ""

    # ------------------------------------------------------------ control ops

    def _op_hold(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        return [], "held for the next flush"

    def _op_noop(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        return [], ""

    def _op_flag_conflict(self, a: dict[str, Any], chunk: Chunk | None, at: str) -> tuple[list[str], str]:
        return [r for r in a.get("record_ids") or [] if envelope.get_record(self.conn, r)], ""

    # ------------------------------------------------------------ structure ops

    def _refine_batch(self, proposals: list[Op]) -> list[tuple[Op, bool, bool, str]]:
        """Ask the librarian prompt once for the concrete DDL of every
        proposed structure op. Returns, per proposal in order: (op to run,
        whether the model's refinement is what runs, whether the model was
        called, decline reason or ""). A proposal the reply does not decide
        runs as proposed; a reply in the old single-object form decides
        proposal 0."""
        payload = {
            "proposed": [{"index": i, "op": op.name, **op.args, "signal": op.signal_kind}
                         for i, op in enumerate(proposals)],
            "schema": envelope.schema_summary(self.conn),
        }
        reply, called = prompting.ask(Component.LIBRARIAN, payload, prompts_dir=self.prompts_dir)
        by_index: dict[int, dict[str, Any]] = {}
        items: list[Any] = []
        if isinstance(reply, dict) and isinstance(reply.get("decisions"), list):
            items = reply["decisions"]
        elif isinstance(reply, list):
            items = reply
        elif isinstance(reply, dict):
            items = [reply]
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index", 0 if len(proposals) == 1 or len(items) == 1 else None)
            if isinstance(idx, int) and 0 <= idx < len(proposals) and idx not in by_index:
                by_index[idx] = item
        out: list[tuple[Op, bool, bool, str]] = []
        for i, proposed in enumerate(proposals):
            item = by_index.get(i)
            if item is None:
                out.append((proposed, False, called, ""))
                continue
            if item.get("decline"):
                out.append((proposed, False, called, str(item.get("reason") or "")))
                continue
            ops, _rejected = parse_ops({k: v for k, v in item.items() if k != "index"})
            ops = [o for o in ops if o.group == "structure"]
            if not ops:
                out.append((proposed, False, called, ""))
                continue
            op = ops[0]
            op.signal_kind = proposed.signal_kind
            out.append((op, True, called, ""))
        return out

    def _structure(self, op: Op, at: str) -> tuple[list[str], str]:
        validate_op(op)
        a = op.args
        if op.name == StructureOp.ADD_FIELD:
            envelope.add_field(self.conn, a["collection"], a["name"], a["type"], at=at)
            return [], f"{a['collection']}.{a['name']} {a['type']}"
        if op.name == StructureOp.ADD_COLLECTION:
            envelope.create_collection(self.conn, a["name"], a["fields"], at=at)
            return [], f"collection {a['name']}"
        if op.name == StructureOp.ADD_INDEX:
            return [], envelope.add_index(self.conn, a["collection"], list(a["fields"]))
        if op.name == StructureOp.CREATE_VIEW:
            envelope.create_view(self.conn, a["name"], a["collection"], a["where"], a.get("order_by"), at=at)
            return [], f"view {a['name']}"
        if op.name == StructureOp.REKEY:
            coll, fld = a["collection"], a["field"]
            note = ""
            if fld not in envelope.columns(self.conn, coll):
                envelope.add_field(self.conn, coll, fld, "TEXT", at=at)
                note = f"added {coll}.{fld} TEXT first"
            changed = envelope.rekey(self.conn, coll, fld, a["assignments"])
            self._rekeyed += changed
            return changed, note
        raise Rejected(f"{op.name} is not a structure op")

    # ------------------------------------------------------------ integrity

    def _signals(self) -> list[IntegritySignal]:
        out: list[IntegritySignal] = []
        conn = self.conn
        for rid in self._new:
            h = self._current_hash.get(rid)
            twins = [o for o, oh in self._current_hash.items() if oh == h and o != rid]
            if h and twins:
                out.append(IntegritySignal(IntegrityKind.DUPLICATE, [twins[0], rid]))
        for rid in dict.fromkeys([*self._new, *self._rekeyed]):
            rec = envelope.get_record(conn, rid)
            if not envelope.is_live(rec) or rec["valid_to"] is not None:
                continue
            flds = envelope.record_fields(rec)
            if not flds:
                continue
            cond = " AND ".join(f'"{k}" = ?' for k in flds)
            rows = conn.execute(
                f'SELECT record_id FROM "{rec["collection"]}" WHERE {cond} AND about = ? AND record_id != ? '
                "AND valid_to IS NULL AND tombstoned_at IS NULL",
                [*flds.values(), json.dumps(rec["about"]), rid],
            ).fetchall()
            for (other,) in rows:
                if self._current_hash.get(other) != self._current_hash.get(rid):
                    out.append(
                        IntegritySignal(IntegrityKind.CONTRADICTION, [other, rid], {"fields": sorted(flds)})
                    )
                    break
        # A coarse field (one value shared by many records) would make every
        # insert "contradict" the store; the processor needs a sample, not a flood.
        kept: list[IntegritySignal] = []
        per_kind: dict[str, int] = {}
        for s in out:
            per_kind[s.kind] = per_kind.get(s.kind, 0) + 1
            if per_kind[s.kind] <= SIGNALS_PER_KIND:
                kept.append(s)
        out = kept
        for detail in self._mismatch.values():
            if detail["field"] not in envelope.columns(conn, detail["collection"]):
                out.append(
                    IntegritySignal(IntegrityKind.SCHEMA_MISMATCH, [detail["record_id"]], dict(detail))
                )
        for gone in self._gone:
            for d in envelope.derived_of(conn, gone):
                rec = envelope.get_record(conn, d)
                if rec and all(
                    not envelope.is_live(envelope.get_record(conn, s)) for s in rec["derived_from"]
                ):
                    out.append(
                        IntegritySignal(
                            IntegrityKind.ORPHANED_DERIVATION, [d], {"sources": rec["derived_from"]}
                        )
                    )
            for r1, r2 in conn.execute(
                "SELECT record_id, other_record_id FROM _links WHERE record_id = ? OR other_record_id = ?",
                (gone, gone),
            ).fetchall():
                if (r1, r2) not in self._reported_links:
                    self._reported_links.add((r1, r2))
                    out.append(IntegritySignal(IntegrityKind.DANGLING_LINK, [r1, r2], {"gone": gone}))
        return out
