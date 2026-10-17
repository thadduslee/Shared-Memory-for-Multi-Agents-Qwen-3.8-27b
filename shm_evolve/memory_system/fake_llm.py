"""A deterministic stand-in for `llm.complete`, so every model-driven path
runs in tests and in `loop.run --fake-llm` without an endpoint.

It answers as the component that asks -- recognised from the `# component:`
header of the system prompt, or the `"component"` key of the JSON input --
with a reply in that component's output contract:

  * front_door: classifies by generic English cue words (the plain phrasings
    the contract tests use: "authorized", "revoked", "delete", "update",
    ...), splits a turn into sentences when they do different jobs, names
    `about` by matching declared ids, and adds a `subject` attribute so the
    schema_mismatch -> add_field -> rekey path is exercised.
  * processor: the processor's own fallback rules, plus a structure response
    to schema_mismatch signals.
  * librarian: accepts every proposed structure op unchanged, by index.
  * retriever: the scaffold's fallback search and selection rules.

This is NOT a model and the cue words are NOT a design: a keyword reading is
exactly what the held-out paraphrase checks are built to fail. It exists so
the plumbing (parse, validate, apply, trace, budget accounting) is exercised
end to end. It charges `llm.usage()` like a real call and returns None over
budget, so budget behaviour is testable too.
"""

from __future__ import annotations

import json
import re
from typing import Any

from memory_system import llm
from memory_system.cues import cue_kind
from memory_system.directory import Directory
from memory_system.ops import Chunk, ChunkKind, extract_json

_SENTENCE = re.compile(r"(?<=[.!?;])\s+")

_REAL = {"complete": None, "enabled": None}


def _sentence_kind(text: str, turn_kind: str) -> str:
    cue = cue_kind(text)
    if cue:
        return cue
    if turn_kind and turn_kind != "dialogue":
        return ChunkKind.DOCUMENT.value
    if text.rstrip().endswith("?"):
        return ChunkKind.QUESTION.value
    return ChunkKind.FACT.value


def _group(facts: list[dict[str, Any]], ids: list[str], directory: Directory) -> set[str]:
    out: set[str] = set()
    for f in facts:
        links = f.get("links") or []
        if not f.get("qualified") and set(links) & set(ids):
            out |= {x for x in links if directory.is_principal(x)}
    return out


def _front_door(p: dict[str, Any]) -> dict[str, Any]:
    turn = p["turn"]
    text = str(turn.get("text") or "")
    turn_kind = str(turn.get("turn_kind") or "")
    speaker = (turn.get("speaker") or {}).get("id", "")
    directory = Directory.from_payload(p.get("declarations") or {})
    sentences = [s for s in _SENTENCE.split(text.strip()) if s.strip()] or [text]
    kinds = [_sentence_kind(s, turn_kind) for s in sentences]
    pieces = list(zip(sentences, kinds, strict=True)) if len(set(kinds)) > 1 else [(text, kinds[0])]
    chunks = []
    for piece, kind in pieces:
        about = directory.mentions(piece)
        entities = [a for a in about if not directory.is_principal(a)]
        audience = sorted(_group(p.get("policy_facts") or [], about, directory) - {speaker})
        attrs = {"subject": (entities or about)[0]} if about else {}
        chunks.append(
            {
                "text": piece,
                "kind": kind,
                "about": about,
                "suggested_audience": [speaker, *audience],
                "attributes": attrs,
            }
        )
    return {"chunks": chunks}


def _processor(p: dict[str, Any]) -> dict[str, Any]:
    from memory_system.processor import fallback_ops

    directory = Directory.from_payload(
        {"principals": [{"id": x} for x in p.get("principals") or []], "entities": p.get("entities") or []}
    )
    ops: list[dict[str, Any]] = []
    for c in p.get("chunks") or []:
        chunk = Chunk(
            chunk_id=c["chunk_id"],
            turn_id=c["turn_id"],
            timestamp=c["timestamp"],
            speaker_id=c["speaker"],
            speaker_role=c["speaker_role"],
            text=c["text"],
            kind=c["kind"],
            about=c["about"],
            suggested_audience=c["suggested_audience"],
            attributes=c["attributes"],
        )
        for op in fallback_ops(chunk, p.get("memory") or [], directory):
            ops.append(
                {
                    "op": op.name,
                    **op.args,
                    "chunk_id": op.chunk_id,
                    **({"signal_kind": op.signal_kind} if op.signal_kind else {}),
                }
            )
    schema = (p.get("schema") or {}).get("collections") or {}
    done: set[tuple[str, str]] = set()
    for s in p.get("signals") or []:
        if s.get("kind") != "schema_mismatch":
            continue
        d = s.get("detail") or {}
        coll, fld, value = d.get("collection"), d.get("field"), d.get("value")
        if not coll or not fld or (coll, fld) in done or fld in (schema.get(coll) or {}).get("fields", {}):
            continue
        done.add((coll, fld))
        assignments = {
            m["record_id"]: value
            for m in p.get("memory") or []
            if m.get("current") and value in (m.get("about") or [])
        }
        if d.get("record_id"):
            assignments[d["record_id"]] = value
        sk = "integrity:schema_mismatch"
        ops += [
            {"op": "add_field", "collection": coll, "name": fld, "type": "TEXT", "signal_kind": sk},
            {"op": "rekey", "collection": coll, "field": fld, "assignments": assignments, "signal_kind": sk},
            {"op": "add_index", "collection": coll, "fields": [fld], "signal_kind": sk},
        ]
    return {"ops": ops}


def _librarian(p: dict[str, Any]) -> Any:
    """Execute every proposed structure op unchanged, one decision per index
    (the batch contract); a lone dict proposal (the old contract) is echoed."""
    proposed = p.get("proposed")
    if isinstance(proposed, list):
        return [{"index": i, **{k: v for k, v in item.items() if k not in ("index", "signal")}}
                for i, item in enumerate(proposed) if isinstance(item, dict)]
    return dict(proposed or {})


def _retriever(p: dict[str, Any]) -> dict[str, Any]:
    from memory_system.retriever import FALLBACK_TOP_K, RELEVANCE_MIN

    if p.get("stage") == "search":
        return {"search": {"terms": list(p.get("mentioned") or []), "current_only": True, "limit": 40}}
    opened = [
        o
        for o in p.get("opened") or []
        if o.get("score", 0) >= RELEVANCE_MIN and o.get("matches_content", True)
    ]
    opened.sort(key=lambda o: (o["current"], o["score"], o["created_at"]), reverse=True)
    if opened:
        chosen = opened[:FALLBACK_TOP_K]
        redacted = all(o.get("granularity") == "summary" for o in chosen)
        return {
            "release": [o["record_id"] for o in chosen],
            "action": "answer_redacted" if redacted else "answer",
        }
    if p.get("n_existence_only"):
        return {"release": [], "action": "answer_redacted"}
    if p.get("n_denied_relevant"):
        return {"release": [], "action": "refuse"}
    return {"release": [], "action": "no_memory"}


_HANDLERS = {
    "front_door": _front_door,
    "processor": _processor,
    "librarian": _librarian,
    "retriever": _retriever,
}
_HEADER = re.compile(r"^\s*#\s*component:\s*([a-z_]+)", re.MULTILINE)


def complete(
    prompt: str, *, system: str = "", max_tokens: int | None = None, temperature: float = 0.0,
    thinking: bool | None = None,
) -> str | None:
    """Same signature and budget behaviour as llm.complete. `thinking` is
    accepted and ignored: the fake has nothing to reason with."""
    tokens, calls = llm.budget_left()
    if tokens <= 0 or calls <= 0:
        llm._USAGE["refused_over_budget"] += 1
        return None
    payload = extract_json(prompt)
    m = _HEADER.search(system or "")
    component = (m.group(1) if m else None) or (payload or {}).get("component")
    handler = _HANDLERS.get(component or "")
    if handler is None or not isinstance(payload, dict):
        return None
    reply = json.dumps(handler(payload))
    llm._USAGE["calls"] += 1
    llm._USAGE["prompt_tokens"] += (len(system) + len(prompt)) // 4
    llm._USAGE["completion_tokens"] += len(reply) // 4
    return reply


def install() -> None:
    """Route the memory system's model calls to this fake."""
    if _REAL["complete"] is None:
        _REAL["complete"], _REAL["enabled"] = llm.complete, llm.enabled
    llm.complete = complete
    llm.enabled = lambda: True


def uninstall() -> None:
    if _REAL["complete"] is not None:
        llm.complete, llm.enabled = _REAL["complete"], _REAL["enabled"]
        _REAL["complete"] = _REAL["enabled"] = None


def installed() -> bool:
    return llm.complete is complete
