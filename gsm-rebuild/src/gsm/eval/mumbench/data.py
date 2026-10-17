"""MUMBench data (AIM paper, arXiv 2609.12320, Appendix A.4-A.6).

The dataset is not released with the paper (no link as of 2026-10-01); this module follows the schema the
paper prints. An interaction is one user message in a chronologically ordered stream shared by many users:

    {"interaction_id", "input", "input_type", "timestamp", "user_id", "agent_id", "run_id",
     "memory_actions": [{"operation": create|read|update|delete|noop,
                         "created_memory" | "retrieved_memories" | "memory_to_update" + "updated_memory"
                         | "memory_to_delete": memory objects}],
     "_metadata": {"domain", ...}}

    memory = {"id", "content", "user_id", "agent_id", "run_id", "tags", "visibility": private|public, ...}

Method side (``load_for_method``) is an **allowlist**: id, input, timestamp, user/agent/run ids and the
domain. ``input_type`` (query vs statement hints at the operation), ``memory_actions`` and the rest of
``_metadata`` stay behind.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

INTERACTION_ALLOWLIST = frozenset({"interaction_id", "input", "timestamp", "user_id", "agent_id", "run_id"})
OPERATIONS = ("create", "read", "update", "delete", "noop")


class MethodInteraction(BaseModel):
    """Everything a method may know about one interaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interaction_id: str
    domain: str
    position: int  # order within the domain stream
    input: str
    timestamp: str | None = None
    user_id: str
    agent_id: str | None = None
    run_id: str | None = None


def _records(path: Path) -> Iterator[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                yield json.loads(line)
        return
    obj = json.loads(text)
    yield from obj if isinstance(obj, list) else obj.get("interactions", [obj])


def load_raw(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Domain -> interactions in file order. One file per domain (``*.json`` or ``*.jsonl``)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted([*data_dir.glob("*.json"), *data_dir.glob("*.jsonl")]):
        for i, rec in enumerate(_records(path)):
            domain = str((rec.get("_metadata") or {}).get("domain") or path.stem)
            rec.setdefault("interaction_id", f"{path.stem}-{i:04d}")
            out.setdefault(domain, []).append(rec)
    return out


def sanitize_interaction(domain: str, position: int, raw: dict[str, Any]) -> MethodInteraction:
    kept = {k: raw[k] for k in INTERACTION_ALLOWLIST if k in raw}
    return MethodInteraction(
        interaction_id=str(kept["interaction_id"]),
        domain=domain,
        position=position,
        input=str(kept.get("input", "")),
        timestamp=kept.get("timestamp"),
        user_id=str(kept["user_id"]),
        agent_id=kept.get("agent_id"),
        run_id=kept.get("run_id"),
    )


def load_for_method(data_dir: Path) -> dict[str, list[MethodInteraction]]:
    """The only loader a method uses. Gold memory actions never leave this function."""
    return {
        domain: [sanitize_interaction(domain, i, r) for i, r in enumerate(recs)]
        for domain, recs in load_raw(data_dir).items()
    }
