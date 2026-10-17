"""The trace: every component decision, in the episode's own SQLite file and
mirrored to `<db>.trace.jsonl` beside it.

The outer loop learns from nothing else. The judge joins a failed checkpoint
to the retriever event that released (or denied) its records, and from those
record ids back to the processor/librarian events that created, granted or
superseded them; `seq` orders all of it.

Two rules shape this module:

  * A trace never carries memory plaintext. The JSONL lives next to the
    database, and "deleted means unrecoverable" covers every file there.
    `redact_args` keeps ids, names and counts and drops text-bearing
    arguments; `scrub` does the same for free-text reasons.
  * `query` must leave the database byte-identical. A retriever event is
    therefore written to the JSONL immediately but held in memory for the
    table, and inserted on the next write (`commit_pending`, called at the
    start of `ingest`/`reset` and by `close`). `events()` returns the held
    events too, so a reader sees the same sequence either way.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from typing import Any

from contracts import TraceEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace (
    seq INTEGER PRIMARY KEY, component TEXT NOT NULL, turn_id TEXT NOT NULL, op TEXT NOT NULL,
    outcome TEXT NOT NULL, chunk_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, op_args TEXT NOT NULL,
    record_ids TEXT NOT NULL, denied_ids TEXT NOT NULL, reason TEXT NOT NULL, llm_called INTEGER NOT NULL,
    signal_kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS trace_by_checkpoint ON trace(checkpoint_id);
"""

_COLS = (
    "seq",
    "component",
    "turn_id",
    "op",
    "outcome",
    "chunk_id",
    "checkpoint_id",
    "op_args",
    "record_ids",
    "denied_ids",
    "reason",
    "llm_called",
    "signal_kind",
)

#: Held (not yet inserted) events per connection; see the module docstring.
_PENDING: dict[int, list[TraceEvent]] = {}

# Arguments that carry memory text (or values cut from it).
_TEXT_KEYS = frozenset({"text", "note", "value", "values", "summary"})
_IDISH = re.compile(r"^[A-Za-z0-9_.:#\-]{1,64}$")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def mirror_path(db_path: str) -> str | None:
    return None if db_path in ("", ":memory:") else f"{db_path}.trace.jsonl"


def _idish(v: Any) -> bool:
    return isinstance(v, (int, float, bool)) or v is None or (isinstance(v, str) and bool(_IDISH.match(v)))


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Op arguments with every text-bearing value replaced by its shape."""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in _TEXT_KEYS:
            out[k] = {"chars": len(v)} if isinstance(v, str) else "<redacted>"
        elif k == "parts" and isinstance(v, list):
            out[k] = [
                {
                    "chars": len(str(p.get("text", ""))) if isinstance(p, dict) else 0,
                    "about": [a for a in (p.get("about") or []) if _idish(a)] if isinstance(p, dict) else [],
                    "audience": [a for a in (p.get("audience") or []) if _idish(a)]
                    if isinstance(p, dict)
                    else [],
                }
                for p in v
            ]
        elif k == "assignments" and isinstance(v, dict):
            out[k] = sorted(str(r) for r in v)
        elif k == "fields" and isinstance(v, dict):
            out[k] = sorted(str(f) for f in v)
        elif k == "fields" and isinstance(v, list):
            out[k] = [
                {"name": f.get("name"), "type": f.get("type")} if isinstance(f, dict) else str(f)
                for f in v
                if isinstance(f, (dict, str)) and (not isinstance(f, str) or _idish(f))
            ]
        elif k == "where" and isinstance(v, list):
            out[k] = [[c[0], c[1], "<literal>"] if isinstance(c, list) and len(c) == 3 else c for c in v]
        elif k == "attributes" and isinstance(v, dict):
            out[k] = sorted(str(a) for a in v)
        elif _idish(v) or (isinstance(v, list) and all(_idish(x) for x in v)):
            out[k] = v
        else:
            out[k] = "<redacted>"
    return out


_QUOTED = re.compile(r"(?<![A-Za-z0-9])(['\"])(.*?)\1(?![A-Za-z0-9])")


def scrub(reason: str) -> str:
    """A reason with any quoted fragment that is not an identifier removed.
    Validation messages quote what the model sent, which may be text."""
    reason = (reason or "")[:240]
    if reason.startswith("not an op object"):
        return "not an op object"
    return _QUOTED.sub(lambda m: m.group(0) if _IDISH.match(m.group(2) or "") else "<text>", reason)


class Tracer:
    """Appends TraceEvents for one episode database."""

    def __init__(self, conn: sqlite3.Connection, db_path: str) -> None:
        self.conn = conn
        self.mirror = mirror_path(db_path)
        ensure_schema(conn)
        row = conn.execute("SELECT MAX(seq) FROM trace").fetchone()
        self._seq = int(row[0] or 0)
        _PENDING.setdefault(id(conn), [])

    def reset_mirror(self) -> None:
        if self.mirror:
            with open(self.mirror, "w", encoding="utf-8"):
                pass

    def emit(self, ev: TraceEvent, *, defer: bool = False) -> TraceEvent:
        self._seq += 1
        ev.seq = self._seq
        ev.op_args = redact_args(ev.op_args)
        ev.reason = scrub(ev.reason)
        if self.mirror:
            with open(self.mirror, "a", encoding="utf-8") as fh:
                fh.write(ev.to_json() + "\n")
        if defer:
            _PENDING.setdefault(id(self.conn), []).append(ev)
        else:
            _insert(self.conn, ev)
        return ev

    def commit_pending(self) -> None:
        held = _PENDING.get(id(self.conn)) or []
        for ev in held:
            _insert(self.conn, ev)
        held.clear()

    def forget(self) -> None:
        _PENDING.pop(id(self.conn), None)


def _insert(conn: sqlite3.Connection, ev: TraceEvent) -> None:
    d = asdict(ev)
    row = [
        d[c] if c not in ("op_args", "record_ids", "denied_ids") else json.dumps(d[c], sort_keys=True)
        for c in _COLS
    ]
    row[_COLS.index("llm_called")] = int(bool(ev.llm_called))
    conn.execute(f"INSERT INTO trace({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})", row)


def events(conn: sqlite3.Connection, **filters: Any) -> list[TraceEvent]:
    """Trace events in seq order, held ones included. Filters are equality on
    TraceEvent fields (e.g. component="retriever", checkpoint_id="c1")."""
    for k in filters:
        if k not in _COLS:
            raise ValueError(f"unknown trace field {k!r}")
    rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM trace ORDER BY seq").fetchall()
    out: list[TraceEvent] = []
    for r in rows:
        d = dict(zip(_COLS, r, strict=True))
        for c in ("op_args", "record_ids", "denied_ids"):
            d[c] = json.loads(d[c])
        d["llm_called"] = bool(d["llm_called"])
        out.append(TraceEvent(**d))
    out += list(_PENDING.get(id(conn)) or [])
    out.sort(key=lambda e: e.seq)
    return [e for e in out if all(getattr(e, k) == v for k, v in filters.items())]
