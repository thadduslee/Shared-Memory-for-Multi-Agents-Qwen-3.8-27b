"""The library interface (SPEC §11.1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from govmem.config import Settings
from govmem.core.ops import Core, Op, OpResult
from govmem.core.store import Store
from govmem.embed import Embedder
from govmem.interpret.interpreter import Interpreter, Turn, TurnOutcome
from govmem.interpret.roster import Roster
from govmem.llm import ChatClient, Health, MemoryModel, ReplyCache
from govmem.organize.organiser import Organiser
from govmem.retrieve.relevance import Relevance
from govmem.retrieve.retriever import Evidence, Retriever

__all__ = ["Memory", "Turn", "Evidence", "Relationship"]


@dataclass
class Relationship:
    type: str
    endpoints: dict[str, str]
    attributes: dict[str, Any]


class Memory:
    def __init__(self, path: str = ":memory:", settings: Settings | None = None, *,
                 chat: Any = "auto", cache_path: Path | None = None, embedder: Embedder | None = None) -> None:
        self.cfg = settings or Settings.from_env()
        self.store = Store(path)
        self.core = Core(self.store, self.cfg.features)
        self.health = Health()
        if chat == "auto":
            chat = ChatClient(self.cfg.memory) if self.cfg.memory.configured else None
        self.model = MemoryModel(chat, call_budget=self.cfg.memory_call_budget,
                                 token_budget=self.cfg.memory_token_budget, health=self.health,
                                 cache=ReplyCache(cache_path))
        self.embedder = embedder or Embedder.shared()
        self.relevance = Relevance(self.store, self.embedder)
        self.roster = Roster(self.store)
        self.interpreter = Interpreter(self.store, self.core, self.roster, self.model, self.cfg, self.embedder,
                                       self.relevance)
        self.organiser = Organiser(self.store, self.core, self.model, self.cfg, self.embedder)
        self.retriever = Retriever(self.store, self.roster, self.relevance, self.cfg)

    @classmethod
    def open(cls, path: str, **kw: Any) -> "Memory":
        return cls(path, **kw)

    # ------------------------------------------------------------ declarations
    def declare(self, principals: list[dict[str, Any]], entities: list[str] | None = None,
                relationships: list[Relationship] | None = None) -> None:
        with self.store.atomic():
            for p in principals:
                self.store.declare_principal(p["id"], p.get("role", ""), p.get("display_name", ""),
                                             p.get("kind", "unknown"), p.get("acts_for"))
            pids = self.store.principal_ids()
            for e in entities or []:
                if e not in pids:
                    self.store.declare_entity(e)
            for rel in relationships or []:
                ends = {k: v for k, v in rel.endpoints.items()}
                ps = [v for v in ends.values() if v in pids]
                es = [v for v in ends.values() if v not in pids]
                for e in es:
                    self.store.declare_entity(e)
                self.store.declare_relationship(rel.type, ends, ps, es, rel.attributes)
                if not rel.attributes and self.cfg.features.unqualified_relationship_access:
                    for p in ps:
                        for other in list(ends.values()):
                            if other != p:
                                self.store.add_scope_fact(other, p, "full", "", "declared")
        self.roster.refresh()

    # ------------------------------------------------------------ write
    def ingest(self, turn: Turn) -> TurnOutcome:
        out = self.interpreter.ingest(turn)
        seq = self.store.turn_seq(turn.turn_id) or 0
        self.organiser.after_turn(turn.turn_id, seq, out.new_records)
        return out

    def apply(self, op: Op) -> OpResult:
        seq = self.store.last_seq()
        return self.core.apply([op], seq)[0]

    # ------------------------------------------------------------ read
    def query(self, asker_id: str, text: str, as_of: str | None = None) -> Evidence:
        seq = self.store.turn_seq(as_of) if as_of else self.store.last_seq()
        return self.retriever.query(asker_id, text, seq if seq is not None else self.store.last_seq(), as_of or "")

    def reorganisations(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.q("SELECT * FROM _reorgs ORDER BY id")]

    def audit(self, since_seq: int = 0) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.q("SELECT * FROM _op_log WHERE seq > ? ORDER BY seq", (since_seq,))]

    def health_report(self) -> dict[str, Any]:
        return {"model": self.health.to_dict(), "store": self.store.health(),
                "model_calls": self.model.calls, "model_tokens": self.model.tokens}

    def close(self) -> None:
        self.store.conn.close()
