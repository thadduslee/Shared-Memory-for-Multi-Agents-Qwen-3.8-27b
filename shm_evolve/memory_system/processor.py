"""Processor: a batch of chunks -> update ops.  (prompted; evolvable)

Inputs (DESIGN.md §2.3): the chunks in order, `peek()` of the memory they
touch (opened with OWNER -- maintenance, not release), and the integrity
signals the librarian computed on its last pass. Output: ops from the closed
vocabulary, parsed by `ops.parse_ops`; anything outside it is traced as
REJECTED, which is itself evidence the outer loop sees.

The scaffold adds two guarantees the prompt cannot remove:

  * COVERAGE. Every chunk in the batch ends up with at least one op. A chunk
    the model's reply never mentions gets the deterministic fallback for its
    kind rather than silently vanishing.
  * PROVENANCE. An op that needs a source chunk (add, split, supersede, hold,
    noop, and grant -- whose authority is checked against it) must name a
    chunk of this batch.

The fallback rules are the ones DESIGN.md and the brief fix. They read only
structure (chunk kind, declared ids, speakers, fields) and word overlap
between a chunk and the peeked records -- never a cue-word list.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from contracts import Component, Outcome, TraceEvent
from memory_system import directory as dirmod
from memory_system import envelope, prompting, terms
from memory_system.directory import Directory
from memory_system.ops import REQUIRED_ARGS, Chunk, ChunkKind, IntegritySignal, Op, parse_ops
from memory_system.retriever import peek
from memory_system.trace import Tracer, scrub

#: Ops whose `chunk_id` must name a chunk of the current batch.
_CHUNK_BOUND = frozenset({"add", "split", "supersede", "hold", "noop", "grant"})


@dataclass
class Planned:
    """One op, with how it was decided."""

    op: Op
    llm_called: bool
    fallback: bool
    reason: str = ""


def _content_terms(text: str, directory: Directory) -> set[str]:
    """Terms of a text minus the tokens that only name declared ids (those
    say who/what, not which fact)."""
    return terms.terms(text) - directory.id_terms()


def _overlap(a: set[str], text: str, directory: Directory) -> int:
    return len(a & _content_terms(text, directory))


#: A dash/underscore-joined compound (a token, code or id: "ks_stg-9Q4M",
#: "bridge-29") or a mixed letter+digit id of some length ("quexilon7781").
#: Deliberately structural, not a word list: it is what a secret VALUE looks
#: like, in any vocabulary, never what it is called. Free-text deletions with
#: no such token (a name, a mapping stated only in prose) do not get this
#: treatment and fall through to the overlap floor below -- a known
#: imprecision (see README's "loose fallback name matching"), not something a
#: regex can safely resolve without risking a false hit on a sentence that
#: merely discusses the same subject in its own words.
_COMPOUND = re.compile(r"[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)+")
_ALNUM_ID = re.compile(r"\b(?=\w*[A-Za-z])(?=\w*\d)\w{5,}\b")


def _strong_keys(text: str, directory: Directory) -> list[str]:
    """Literal spans worth an exact substring match, extracted from a
    deletion chunk. A record containing one of these is almost certainly the
    thing the chunk names; generic keyword overlap (the fallback below) can
    equally be satisfied by a neighbour that shares the same nouns ("the
    customer mapping", "the project") but a different value, which is
    exactly the over-deletion this is here to prevent.

    A declared id can itself be dash/underscore-joined ("thing_beta"); that
    says what a record is ABOUT, same as any other id mention, not which
    fact -- so it is excluded here exactly as `_content_terms` excludes it
    from the overlap score, or every record about the same thing would look
    like a strong match."""
    keys = {m.group(0) for m in _COMPOUND.finditer(text)} | {m.group(0) for m in _ALNUM_ID.finditer(text)}
    return [
        k for k in keys
        if len(k) >= 4 and not directory.is_declared(k) and k.lower() not in directory.id_terms()
    ]


def _strong_matches(keys: list[str], memory: list[dict[str, Any]]) -> list[str]:
    return [m["record_id"] for m in memory if any(k.lower() in m["text"].lower() for k in keys)]


def fallback_ops(chunk: Chunk, memory: list[dict[str, Any]], directory: Directory) -> list[Op]:
    """The deterministic reading of one chunk (DESIGN.md §2.3 fallback)."""
    cid = chunk.chunk_id
    kind = chunk.kind
    mine = _content_terms(chunk.text, directory)
    live_current = [m for m in memory if m.get("current")]
    targets = [a for a in chunk.about if directory.is_principal(a) and a != chunk.speaker_id]
    scope = [a for a in chunk.about if a not in targets and a != chunk.speaker_id] or [chunk.speaker_id]

    if kind in (ChunkKind.FACT, ChunkKind.DOCUMENT):
        return [Op("add", {"chunk_id": cid}, cid)]

    if kind == ChunkKind.CORRECTION:
        attr_keys = set(chunk.attributes)
        pool = [m for m in live_current if attr_keys & set(m.get("fields") or {})] or [
            m for m in live_current if set(m.get("about") or []) & set(chunk.about)
        ]
        best = max(pool, key=lambda m: (_overlap(mine, m["text"], directory), m["created_at"]), default=None)
        if best is not None and _overlap(mine, best["text"], directory) > 0:
            return [
                Op("supersede", {"old_record_id": best["record_id"], "chunk_id": cid}, cid, "instruction")
            ]
        return [Op("add", {"chunk_id": cid}, cid)]

    if kind == ChunkKind.CONSENT:
        # Grant only, not add: a stored consent sentence shares words with the
        # very records it opens and would compete with them for a later
        # correction or question.
        ops: list[Op] = []
        own = [
            m
            for m in live_current
            if m["speaker"] == chunk.speaker_id or chunk.speaker_id in (m.get("about") or [])
        ]
        scored = [(m, _overlap(mine, m["text"], directory)) for m in own]
        top = max((s for _, s in scored), default=0)
        picked = [m["record_id"] for m, s in scored if top > 0 and s == top]
        if targets and picked:
            ops.append(
                Op(
                    "grant",
                    {
                        "record_ids": picked,
                        "principal_ids": targets,
                        "authority": chunk.speaker_id,
                        "chunk_id": cid,
                    },
                    cid,
                    "instruction",
                )
            )
        return ops or [Op("noop", {"chunk_id": cid}, cid)]

    if kind in (ChunkKind.REVOKE, ChunkKind.RESTRICT):
        if not targets:
            return [Op("noop", {"chunk_id": cid}, cid)]
        rids = [m["record_id"] for m in memory if set(m.get("about") or []) & set(scope)]
        args: dict[str, Any] = {"record_ids": rids, "principal_ids": targets, "scope": scope, "chunk_id": cid}
        if kind == ChunkKind.REVOKE:
            return [Op("revoke", args, cid, "instruction")]
        return [Op("restrict", args | {"granularity": "summary"}, cid, "instruction")]

    if kind == ChunkKind.DELETE:
        strong = _strong_matches(_strong_keys(chunk.text, directory), memory)
        if strong:
            # An exact hit on a leak-bearing token or quoted span is decisive:
            # skip the overlap floor below, which would otherwise also catch
            # a neighbour that merely shares the same nouns.
            return [Op("shred", {"record_ids": sorted(set(strong)), "chunk_id": cid}, cid, "instruction")]
        scored = [(m, _overlap(mine, m["text"], directory)) for m in memory]
        top = max((s for _, s in scored), default=0)
        if top <= 0:
            return [Op("noop", {"chunk_id": cid}, cid)]
        floor = max(1, math.ceil(top / 2))
        rids = [m["record_id"] for m, s in scored if s >= floor]
        return [Op("shred", {"record_ids": rids, "chunk_id": cid}, cid, "instruction")]

    return [Op("noop", {"chunk_id": cid}, cid)]


def _chunk_view(c: Chunk) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "turn_id": c.turn_id,
        "timestamp": c.timestamp,
        "speaker": c.speaker_id,
        "speaker_role": c.speaker_role,
        "kind": c.kind,
        "about": c.about,
        "suggested_audience": c.suggested_audience,
        "attributes": c.attributes,
        "text": c.text,
    }


def _signal_view(s: IntegritySignal) -> dict[str, Any]:
    return {"kind": s.kind, "record_ids": s.record_ids, "detail": s.detail}


def _fill_chunk_id(reply: Any, cid: str) -> None:
    """With one chunk in the batch there is only one chunk an op can mean; a
    reply that leaves out a required chunk_id is not rejected for it."""
    items = reply.get("ops") if isinstance(reply, dict) and "ops" in reply else reply
    for item in items if isinstance(items, list) else [items]:
        if isinstance(item, dict) and "chunk_id" in REQUIRED_ARGS.get(str(item.get("op")), ()):
            item.setdefault("chunk_id", cid)


def process(
    chunks: list[Chunk],
    *,
    conn: sqlite3.Connection,
    directory: Directory,
    tracer: Tracer,
    signals: list[IntegritySignal],
    allow_hold: bool = True,
    prompts_dir: str | None = None,
) -> list[Planned]:
    if not chunks:
        return []
    about = sorted({a for c in chunks for a in c.about})
    signal_ids = [r for s in signals for r in s.record_ids]
    memory = peek(conn, texts=[c.text for c in chunks], about=about, extra_ids=signal_ids)
    payload = {
        "chunks": [_chunk_view(c) for c in chunks],
        "memory": memory,
        "signals": [_signal_view(s) for s in signals],
        "schema": envelope.schema_summary(conn),
        "principals": list(directory.principals),
        "entities": list(directory.entities),
        "policy_facts": dirmod.compact(dirmod.current_facts(conn, involving=about)) if about else [],
        "hold_allowed": allow_hold,
    }
    reply, called = prompting.ask(Component.PROCESSOR, payload, prompts_dir=prompts_dir)
    by_id = {c.chunk_id: c for c in chunks}
    turn_of = chunks[-1].turn_id
    planned: list[Planned] = []
    if reply is not None:
        default_cid = chunks[0].chunk_id if len(chunks) == 1 else ""
        if default_cid:
            _fill_chunk_id(reply, default_cid)
        ops, rejected = parse_ops(reply, default_chunk_id=default_cid)
        for why in rejected:
            tracer.emit(
                TraceEvent(
                    component=Component.PROCESSOR,
                    turn_id=turn_of,
                    op=why.split(":", 1)[0][:32],
                    outcome=Outcome.REJECTED,
                    reason=scrub(why),
                    llm_called=True,
                )
            )
        for op in ops:
            if op.name in _CHUNK_BOUND and op.chunk_id not in by_id:
                tracer.emit(
                    TraceEvent(
                        component=Component.PROCESSOR,
                        turn_id=turn_of,
                        op=op.name,
                        outcome=Outcome.REJECTED,
                        op_args=op.args,
                        llm_called=True,
                        reason="op names no chunk of this batch",
                    )
                )
                continue
            if op.name == "hold" and not allow_hold:
                continue  # the uncovered chunk falls through to the fallback below
            if not op.signal_kind and op.chunk_id in by_id and by_id[op.chunk_id].kind != ChunkKind.FACT:
                op.signal_kind = "instruction" if op.group == "policy" else ""
            planned.append(Planned(op, llm_called=True, fallback=False))
    covered = {p.op.chunk_id for p in planned}
    for c in chunks:
        if c.chunk_id in covered:
            continue
        why = "model unavailable" if reply is None else "model left this chunk uncovered"
        for op in fallback_ops(c, memory, directory):
            planned.append(Planned(op, llm_called=called, fallback=True, reason=why))
    held = {p.op.chunk_id for p in planned if p.op.name == "hold"}
    if held:
        planned = [p for p in planned if p.op.chunk_id not in held or p.op.name == "hold"]
    return planned
