"""Front door: one turn in, a list of chunks out.  (prompted; evolvable)

The prompt decides how to split a turn and what each piece is. The scaffold
decides what any answer is allowed to do, whatever the prompt says:

  * SPLIT BEFORE CLASSIFY. Chunks are verbatim spans of the turn. A reply
    whose chunk text is not found in the turn is rejected whole: a memory
    that stores the model's paraphrase stores something nobody said, and the
    answerer attributes evidence to a speaker.
  * DEFAULT PRIVATE. The speaker is always in a chunk's audience; with no
    suggestion the audience is the speaker alone. Only declared principals
    can be suggested.
  * TIGHTENING is a property of the kind (ops.TIGHTENING_KINDS), not of a
    flag the model sets, so a model cannot route a widening around the queue.

The deterministic fallback (model disabled, over budget, or rejected) keeps
one chunk per turn. Its kind comes first from `memory_system.cues` -- generic
English phrasings for consent/revocation/restriction/deletion/correction,
none of them dataset vocabulary (see cues.py's docstring and
tests/test_vocabulary_lint.py, which is the arbiter) -- and only when no cue
matches does it fall back to pure structure: the turn's declared `turn_kind`,
a trailing question mark, and which declared ids the text mentions. This is
still not a reading of the instruction, only of the word that announces it;
a paraphrase that avoids every cue is exactly what the prompted front door is
for. Its suggested audience is the speaker plus the holders of unqualified
policy facts linking what the turn mentions (directory.group), which is the
safe reading of the declarations.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from contracts import Component, Outcome, TraceEvent
from memory_system import directory as dirmod
from memory_system import prompting
from memory_system.cues import cue_kind
from memory_system.directory import Directory
from memory_system.ops import Chunk, ChunkKind, is_identifier
from memory_system.trace import Tracer

_SPACE = re.compile(r"\s+")
_EDGE = " \t\n\"'`.,;:!?()[]{}-"


def _norm(text: str) -> str:
    return _SPACE.sub(" ", (text or "").lower()).strip(_EDGE)


def _speaker(turn: dict[str, Any]) -> tuple[str, str]:
    sp = turn.get("speaker") or {}
    return str(sp.get("principal_id") or ""), str(sp.get("role") or "")


def structural_audience(conn: sqlite3.Connection, speaker_id: str, about: list[str]) -> list[str]:
    """Speaker, plus whoever an unqualified policy fact connects to what the
    chunk is about. Mentioning a principal does NOT add them: "do not tell X"
    mentions X."""
    members = dirmod.group(conn, about) if about else set()
    return [speaker_id] + sorted(m for m in members if m != speaker_id)


def fallback_chunks(turn: dict[str, Any], conn: sqlite3.Connection, directory: Directory) -> list[Chunk]:
    speaker_id, speaker_role = _speaker(turn)
    text = str(turn.get("text") or "")
    turn_kind = str(turn.get("turn_kind") or "")
    cue = cue_kind(text)
    if cue:
        kind = ChunkKind(cue)
    elif turn_kind and turn_kind != "dialogue":
        kind = ChunkKind.DOCUMENT
    elif text.rstrip().endswith("?"):
        kind = ChunkKind.QUESTION
    else:
        kind = ChunkKind.FACT
    about = directory.mentions(text)
    return [
        Chunk(
            chunk_id=f"{turn.get('turn_id')}#0",
            turn_id=str(turn.get("turn_id")),
            timestamp=str(turn.get("timestamp")),
            speaker_id=speaker_id,
            speaker_role=speaker_role,
            text=text,
            kind=kind.value,
            about=about,
            suggested_audience=structural_audience(conn, speaker_id, about),
            turn_kind=turn_kind,
        )
    ]


def _attributes(raw: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        key = re.sub(r"[^a-z0-9_]+", "_", str(k).lower()).strip("_")
        if is_identifier(key) and isinstance(v, (str, int, float)) and str(v).strip():
            out[key] = str(v)[:120]
    return out


def _from_model(reply: Any, turn: dict[str, Any], directory: Directory) -> tuple[list[Chunk] | None, str]:
    """Validated chunks from a model reply, or (None, why it was rejected)."""
    items = reply.get("chunks") if isinstance(reply, dict) else reply
    if not isinstance(items, list) or not items:
        return None, "reply has no chunks list"
    speaker_id, speaker_role = _speaker(turn)
    whole = _norm(str(turn.get("text") or ""))
    chunks: list[Chunk] = []
    for i, item in enumerate(items[:12]):
        if not isinstance(item, dict):
            return None, f"chunk {i} is not an object"
        text = str(item.get("text") or "").strip()
        if not text or _norm(text) not in whole:
            return None, f"chunk {i} text is not a verbatim span of the turn"
        kind = str(item.get("kind") or "").lower()
        if kind not in set(ChunkKind):
            return None, f"chunk {i} kind not in the closed set"
        about = [a for a in (item.get("about") or []) if isinstance(a, str) and directory.is_declared(a)]
        audience = [
            a
            for a in (item.get("suggested_audience") or [])
            if isinstance(a, str) and directory.is_principal(a)
        ]
        chunks.append(
            Chunk(
                chunk_id=f"{turn.get('turn_id')}#{i}",
                turn_id=str(turn.get("turn_id")),
                timestamp=str(turn.get("timestamp")),
                speaker_id=speaker_id,
                speaker_role=speaker_role,
                text=text,
                kind=kind,
                about=list(dict.fromkeys(about)),
                suggested_audience=[speaker_id] + [a for a in dict.fromkeys(audience) if a != speaker_id],
                attributes=_attributes(item.get("attributes")),
                turn_kind=str(turn.get("turn_kind") or ""),
            )
        )
    return chunks, ""


def split(
    turn: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    directory: Directory,
    tracer: Tracer,
    prompts_dir: str | None = None,
) -> list[Chunk]:
    speaker_id, speaker_role = _speaker(turn)
    text = str(turn.get("text") or "")
    mentioned = directory.mentions(text)
    payload = {
        "turn": {
            "turn_id": turn.get("turn_id"),
            "timestamp": turn.get("timestamp"),
            "turn_kind": turn.get("turn_kind") or "",
            "speaker": {"id": speaker_id, "role": speaker_role},
            "text": text,
        },
        "declarations": directory.payload(),
        "mentioned": mentioned,
        "policy_facts": dirmod.compact(dirmod.current_facts(conn, involving=[speaker_id, *mentioned])),
    }
    reply, called = prompting.ask(Component.FRONT_DOOR, payload, prompts_dir=prompts_dir)
    chunks, why = (None, "model unavailable") if reply is None else _from_model(reply, turn, directory)
    outcome = Outcome.APPLIED
    if chunks is None:
        if called:
            tracer.emit(
                TraceEvent(
                    component=Component.FRONT_DOOR,
                    turn_id=str(turn.get("turn_id")),
                    op="chunk",
                    outcome=Outcome.REJECTED,
                    reason=why,
                    llm_called=True,
                )
            )
        chunks = fallback_chunks(turn, conn, directory)
        outcome = Outcome.FALLBACK
    for c in chunks:
        tracer.emit(
            TraceEvent(
                component=Component.FRONT_DOOR,
                turn_id=c.turn_id,
                chunk_id=c.chunk_id,
                op=f"chunk:{c.kind}",
                outcome=outcome,
                llm_called=called,
                op_args={
                    "kind": c.kind,
                    "about": c.about,
                    "audience": c.suggested_audience,
                    "attributes": c.attributes,
                    "turn_kind": c.turn_kind,
                    "n_chunks": len(chunks),
                    "tightening": c.is_tightening,
                },
                signal_kind="instruction" if c.kind in _INSTRUCTION_KINDS else "",
            )
        )
    return chunks


_INSTRUCTION_KINDS = frozenset(
    {ChunkKind.CONSENT, ChunkKind.REVOKE, ChunkKind.RESTRICT, ChunkKind.DELETE, ChunkKind.CORRECTION}
)
