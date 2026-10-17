"""The benchmark-facing interface: reset / ingest / query (TASK.md).

The fixed scaffold of DESIGN.md §2, wired:

    turn -> front_door -> chunks -+- tightening -> processor -> librarian   (now)
                                  +- the rest   -> queue -flush-> processor -> librarian
    query -> flush -> retriever (candidates -> GATE as the asker -> select)

`_visible` strips the benchmark's annotation fields and stays in the path:
the system never reads them.

Writes happen in `reset` and `ingest` only. `query` flushes the queue first
(a no-op when it is empty, which with the default QUEUE_BATCH=1 it always
is after an ingest), then reads; its trace event is held in memory until the
next ingest, so the database file is byte-identical across a query.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from contracts import ACTIONS, HIDDEN_FIELDS, TraceEvent
from memory_system import directory as dirmod
from memory_system import envelope, front_door, llm, processor, trace
from memory_system.directory import Directory
from memory_system.librarian import Librarian
from memory_system.ops import Chunk, ChunkKind, IntegritySignal
from memory_system.queue import ChunkQueue
from memory_system.retriever import Retriever
from memory_system.trace import Tracer

__all__ = ["ACTIONS", "GateMemAgent", "_visible"]


def _visible(checkpoint: Any) -> dict[str, Any]:
    """Project a checkpoint down to the fields the system may read."""
    if not isinstance(checkpoint, dict):
        checkpoint = {
            key: getattr(checkpoint, key)
            for key in ("checkpoint_id", "episode_id", "as_of_turn_id", "query_text")
            if hasattr(checkpoint, key)
        } | {
            "asker": {
                "principal_id": getattr(checkpoint, "asker_principal_id", ""),
                "role": getattr(checkpoint, "asker_role", ""),
            }
        }
    return {k: v for k, v in checkpoint.items() if k not in HIDDEN_FIELDS}


class GateMemAgent:
    def __init__(
        self, db_path: str = ":memory:", *, prompts_dir: str | None = None, queue_batch: int | None = None
    ) -> None:
        self.db_path = str(db_path)
        self.prompts_dir = prompts_dir
        self._queue_batch = queue_batch
        self.conn = None
        self.tracer: Tracer | None = None
        self._open(fresh=False)
        self.directory = Directory.from_entities({})
        self._wire()

    # ------------------------------------------------------------ lifecycle

    def _open(self, *, fresh: bool) -> None:
        if self.conn is not None:
            if self.tracer is not None:
                self.tracer.forget()
            self.conn.close()
        if fresh and self.db_path != ":memory:":
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(self.db_path + suffix).unlink(missing_ok=True)
        self.conn = envelope.connect(self.db_path)
        envelope.ensure_schema(self.conn)
        self.tracer = Tracer(self.conn, self.db_path)
        self.conn.commit()

    def _wire(self) -> None:
        self.queue = ChunkQueue(**({"batch": self._queue_batch} if self._queue_batch else {}))
        self.librarian = Librarian(self.conn, self.directory, self.tracer, self.prompts_dir)
        self.retriever = Retriever(self.conn, self.directory, self.tracer, self.prompts_dir)
        self._signals: list[IntegritySignal] = []
        self._turn_ts: dict[str, str] = {}
        self._last_ts = ""

    def close(self) -> None:
        """Write held trace events and close. Safe to call twice."""
        if self.conn is not None:
            self.tracer.commit_pending()
            self.conn.commit()
            self.tracer.forget()
            self.conn.close()
            self.conn = None

    def reset(self, episode: dict[str, Any]) -> None:
        llm.new_episode()
        self._open(fresh=True)
        self.tracer.reset_mirror()
        self.directory = Directory.from_entities((episode or {}).get("entities"))
        dirmod.seed_policy(self.conn, self.directory)
        self._wire()
        self.conn.commit()

    # ------------------------------------------------------------ ingest

    def _process(self, chunks: list[Chunk], allow_hold: bool) -> set[str]:
        planned = processor.process(
            chunks,
            conn=self.conn,
            directory=self.directory,
            tracer=self.tracer,
            signals=self._signals,
            allow_hold=allow_hold,
            prompts_dir=self.prompts_dir,
        )
        self._signals = self.librarian.apply(planned, chunks)
        return {p.op.chunk_id for p in planned if p.op.name == "hold"}

    def _apply_due_expiries(self, now: str) -> None:
        due = self.conn.execute(
            "SELECT rowid, record_id, principal_id FROM _expiries WHERE applied = 0 AND at <= ?", (now,)
        ).fetchall()
        for rowid, rid, pid in due:
            self.librarian.withdraw(rid, pid)
            self.conn.execute("UPDATE _expiries SET applied = 1 WHERE rowid = ?", (rowid,))

    def ingest(self, turn: Any) -> None:
        turn = dict(turn or {})
        self.tracer.commit_pending()
        ts = str(turn.get("timestamp") or self._last_ts)
        turn["timestamp"] = ts
        self._turn_ts[str(turn.get("turn_id"))] = ts
        self._last_ts = max(self._last_ts, ts)
        self._apply_due_expiries(ts)
        chunks = front_door.split(
            turn, conn=self.conn, directory=self.directory, tracer=self.tracer, prompts_dir=self.prompts_dir
        )
        tightening = [c for c in chunks if c.is_tightening]
        if any(c.kind == ChunkKind.DELETE for c in tightening):
            # A deletion must also reach content still waiting in the queue:
            # store it first, then forget it. (Storing is not a widening a
            # revocation could be overtaken by; the ledger covers grants.)
            self.queue.flush(self._process, final=True)
        for c in tightening:
            self._process([c], False)
        for c in chunks:
            if not c.is_tightening:
                self.queue.push(c)
        if self.queue.ready():
            self.queue.flush(self._process)
        self.conn.commit()

    # ------------------------------------------------------------ query

    def query(self, checkpoint: Any) -> dict[str, Any]:
        cp = _visible(checkpoint)
        if len(self.queue):
            self.queue.flush(self._process, final=True)
            self.conn.commit()
        as_of = self._turn_ts.get(str(cp.get("as_of_turn_id") or ""), self._last_ts)
        result = self.retriever.answer(cp, as_of)
        result.debug["memory_llm"] = llm.usage()
        return result.to_dict()

    # ------------------------------------------------------------ helpers for the loop and tests

    def trace_events(self, **filters: Any) -> list[TraceEvent]:
        """This episode's trace, held query events included (trace.events)."""
        return trace.events(self.conn, **filters)

    def schema(self) -> dict[str, Any]:
        return envelope.schema_summary(self.conn)

    def __del__(self) -> None:  # best effort; the evaluator should call close()
        with contextlib.suppress(Exception):
            self.close()
