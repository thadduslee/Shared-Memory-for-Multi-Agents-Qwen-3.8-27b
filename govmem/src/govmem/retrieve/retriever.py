"""Retrieval and disclosure (SPEC §9). `query` changes no access state; it may only write `_signals`/`_health`.

Order: candidates -> relevance (before any policy) -> link expansion -> GATE (keyring, asker's identity) ->
projection -> budget -> attribution -> suggested action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from govmem.config import Settings
from govmem.core.store import Store
from govmem.interpret.roster import Roster
from govmem.retrieve.relevance import Relevance


@dataclass
class Evidence:
    action: str
    items: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)

    @property
    def record_ids(self) -> list[str]:
        return [i["record_id"] for i in self.items]


class Retriever:
    def __init__(self, store: Store, roster: Roster, relevance: Relevance, settings: Settings) -> None:
        self.s, self.roster, self.rel, self.cfg = store, roster, relevance, settings
        self.f = settings.features

    def candidates(self, as_of_seq: int) -> list[str]:
        rows = self.s.q("SELECT id FROM records WHERE tombstoned_at IS NULL AND created_seq <= ? "
                        "AND (superseded_seq IS NULL OR superseded_seq > ?) ORDER BY created_seq, id",
                        (as_of_seq, as_of_seq))
        return [r["id"] for r in rows]

    def query(self, asker: str, text: str, as_of_seq: int, turn_id: str = "") -> Evidence:
        cands = self.candidates(as_of_seq)
        scores = self.rel.score(text, cands) if cands else {}
        floor = self.cfg.relevance_floor
        ranked = sorted(cands, key=lambda r: -scores.get(r, 0.0))
        responsive = [r for r in ranked if scores.get(r, 0.0) >= floor][:30] if self.f.relevance_before_policy \
            else ranked[:30]
        views = {r: self.s.view_of(r, asker) for r in cands}
        opened_ids = [r for r in cands if views[r] in ("full", "redacted")]
        resp_open_full = [r for r in responsive if views[r] == "full"]
        resp_open_red = [r for r in responsive if views[r] == "redacted"]
        resp_denied = [r for r in responsive if views[r] in ("none", "existence")]

        # budget: everything the asker may read when it fits, else ranked (thread-first, link expansion)
        texts: dict[str, tuple[str, str]] = {}
        total_chars = 0
        for r in opened_ids:
            t, v = self.s.open_as(r, asker)              # GATE: keyring with the asker's identity
            if t:
                texts[r] = (t, v)
                total_chars += len(t)
        mode = "full_context"
        if not (self.f.full_context_budget and len(texts) <= self.cfg.budget_records
                and total_chars <= self.cfg.budget_chars):
            mode = "ranked"
            chosen = [r for r in ranked if r in texts][: self.cfg.ranked_top_k]
            if self.f.link_expansion:
                chosen += [r for r in self._expand(chosen[:10], asker) if r in texts and r not in chosen]
            release = sorted(dict.fromkeys(chosen), key=lambda r: self.s.record(r).created_seq)  # type: ignore[union-attr]
        else:
            release = list(texts)
        items = []
        for r in release:
            rec = self.s.record(r)
            t, v = texts[r]
            items.append({"record_id": r, "principal_id": rec.speaker_id, "role": self.s.role_of(rec.speaker_id),  # type: ignore[union-attr]
                          "text": t, "view": v, "timestamp": rec.created_at, "source_turn": rec.source_turn})  # type: ignore[union-attr]
        if resp_open_full:
            action = "answer"
        elif resp_open_red:
            action = "answer_redacted"
        elif resp_denied:
            action = "refuse"
        else:
            action = "no_memory"
        if self.f.read_time_signals and not (resp_open_full or resp_open_red):
            self.s.signal(turn_id, "read_denied" if resp_denied else "read_empty",
                          {"asker": asker, "n_denied": len(resp_denied)})
        self.s.bump(f"query.mode.{mode}")
        counters = {"n_candidates": len(cands), "n_allowed": len(texts), "n_released": len(items),
                    "n_responsive": len(responsive), "n_responsive_full": len(resp_open_full),
                    "n_responsive_redacted": len(resp_open_red), "n_responsive_denied": len(resp_denied),
                    "mode": mode, "chars": sum(len(i["text"]) for i in items)}
        return Evidence(action=action, items=items, counters=counters)

    def _expand(self, seeds: list[str], asker: str, max_depth: int = 1) -> list[str]:
        """Follow active links the asker can see (both ends readable), then cluster mates."""
        out: list[str] = []
        frontier = list(seeds)
        for _ in range(max_depth):
            nxt = []
            for r in frontier:
                for row in self.s.links_of(r):
                    other = row["b"] if row["a"] == r else row["a"]
                    if row["kind"] == "contradicts":
                        continue
                    if self.s.view_of(r, asker) in ("full", "redacted") and \
                            self.s.view_of(other, asker) in ("full", "redacted") and other not in out:
                        out.append(other)
                        nxt.append(other)
            frontier = nxt
        for r in seeds:
            for row in self.s.q("SELECT m2.record_id FROM cluster_members m1 JOIN cluster_members m2 "
                                "ON m1.cluster_id = m2.cluster_id WHERE m1.record_id=? AND m2.record_id != ?", (r, r)):
                o = row["record_id"]
                if o not in out and self.s.view_of(o, asker) in ("full", "redacted"):
                    out.append(o)
        return out[:20]
