"""GateMem's fixed agent interface over the govmem library (SPEC §11.2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from govmem.api import Memory, Relationship
from govmem.adapters.gatemem.data import visible_checkpoint, visible_episode, visible_turn
from govmem.config import Settings
from govmem.interpret.interpreter import Turn


def map_relationship(rel: dict[str, Any], principal_ids: set[str]) -> Relationship:
    """Generic: endpoint = any string field naming a declared principal or ending in `_id`; the rest are
    attributes. Nothing about relationship types is assumed."""
    endpoints, attrs = {}, {}
    for k, v in rel.items():
        if k == "type":
            continue
        if isinstance(v, str) and (v in principal_ids or str(k).lower().endswith("_id")):
            endpoints[k] = v
        else:
            attrs[k] = v
    return Relationship(type=str(rel.get("type") or ""), endpoints=endpoints, attributes=attrs)


class GateMemAgent:
    def __init__(self, db_path: str = ":memory:", settings: Settings | None = None, *, chat: Any = "auto",
                 cache_path: Path | None = None) -> None:
        self.db_path = db_path
        self.settings = settings or Settings.from_env()
        self.chat = chat
        self.cache_path = cache_path
        self.mem: Memory | None = None

    def reset(self, episode: dict[str, Any]) -> None:
        ep = visible_episode(episode)
        if self.mem is not None:
            self.mem.close()
        if self.db_path != ":memory:":
            for suffix in ("", "-wal", "-shm", "-journal"):
                p = Path(self.db_path + suffix)
                if p.exists():
                    os.remove(p)
        self.mem = Memory(self.db_path, self.settings, chat=self.chat, cache_path=self.cache_path)
        principals = [{"id": p["principal_id"], "role": p.get("role", ""), "display_name": p.get("display_name", "")}
                      for p in ep["entities"]["principals"]]
        pids = {p["id"] for p in principals}
        rels = [map_relationship(r, pids) for r in ep["entities"]["relationships"] if isinstance(r, dict)]
        self.mem.declare(principals, relationships=rels)

    def ingest(self, turn: dict[str, Any]) -> None:
        assert self.mem is not None
        t = visible_turn(turn)
        self.mem.ingest(Turn(turn_id=t["turn_id"], timestamp=t.get("timestamp") or "",
                             speaker_id=t["speaker"]["principal_id"], text=t.get("text") or "",
                             kind_hint=t.get("turn_kind") or ""))

    def query(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        assert self.mem is not None
        cp = visible_checkpoint(checkpoint)
        ev = self.mem.query(cp["asker"]["principal_id"], cp.get("query_text") or "", cp.get("as_of_turn_id"))
        return {
            "action": ev.action, "answer": "", "used_record_ids": ev.record_ids,
            "evidence": [{k: i[k] for k in ("record_id", "principal_id", "role", "text")} for i in ev.items],
            "debug": {"retrieval": ev.counters, "memory_llm": {"calls": self.mem.model.calls,
                                                               "tokens": self.mem.model.tokens}},
        }
