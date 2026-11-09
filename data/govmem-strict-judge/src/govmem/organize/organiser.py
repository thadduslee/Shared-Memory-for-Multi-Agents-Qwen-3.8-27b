"""Self-organisation within a conversation (SPEC §8): enrich, link, evolve neighbours, cluster, consolidate.

A-Mem's three steps, with four changes: records are never rewritten (notes are versioned), organisation
never moves information to people who may not read it (§8.4), every reorganisation carries evidence and a
status (§8.5), and consolidation confirms or retracts earlier reorganisations from later evidence (§8.7).

§8.4 is enforced by construction: the existing records shown to the model in one call are only those whose
readers include every reader of the new records (so a new record's note may draw on them), and a neighbour's
note may be updated from the new records only if its readers are a subset of theirs (checked by the core).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from govmem.config import Settings
from govmem.core import keyring
from govmem.core.ops import LINK_KINDS, Core, Op
from govmem.core.store import Store
from govmem.embed import Embedder
from govmem.llm import MemoryModel
from govmem.text import tokens

PROMPTS = Path(__file__).parent / "prompts"
CLUSTER_KINDS = ("same_topic", "same_value_slot", "follow_up", "refers_to", "part_of")


class Organiser:
    def __init__(self, store: Store, core: Core, model: MemoryModel, settings: Settings, embedder: Embedder) -> None:
        self.s, self.core, self.m, self.cfg, self.e = store, core, model, settings, embedder
        self.f = settings.features
        self._prompt = (PROMPTS / "organise.md").read_text(encoding="utf-8")
        self._since_consolidation = 0

    # ================================================================ per turn
    def after_turn(self, turn_id: str, seq: int, new_ids: list[str]) -> None:
        if not self.f.organise:
            return
        alive = [r for r in new_ids if (rec := self.s.record(r)) is not None and rec.tombstoned_at is None]
        if alive and (self.f.enrich or self.f.link or self.f.evolve_neighbours):
            self._organise(turn_id, seq, alive)
        self._since_consolidation += 1
        if self.f.consolidate and self._since_consolidation >= self.cfg.cluster_every:
            self.consolidate(turn_id, seq)

    def _neighbours(self, new_ids: list[str]) -> list[str]:
        union: set[str] = set()
        for r in new_ids:
            union |= self.s.readers(r)
        pool = [r.id for r in self.s.records() if r.id not in new_ids]
        pool = [r for r in pool if union <= self.s.readers(r)]
        if not pool:
            return []
        embs = self.s.embeddings(pool + new_ids)
        newv = [embs[r] for r in new_ids if r in embs]
        if not newv:
            return []
        scored = []
        for r in pool:
            v = embs.get(r)
            if v is not None:
                scored.append((max(float(np.dot(v, n)) for n in newv), r))
        scored.sort(reverse=True)
        k = max(1, self.cfg.neighbours_k)
        return [r for _s, r in scored[: k + 2]]

    def _organise(self, turn_id: str, seq: int, new_ids: list[str]) -> None:
        neigh = self._neighbours(new_ids) if (self.f.link or self.f.evolve_neighbours) else []
        texts = {r: self.s.text_owner(r) or "" for r in new_ids + neigh}
        new_set, neigh_set = set(new_ids), set(neigh)

        def check(obj: dict) -> str | None:
            for key in ("notes", "links", "updates"):
                if not isinstance(obj.get(key, []), list):
                    return f"{key} must be a list"
            for n in obj.get("notes", []):
                if not isinstance(n, dict) or n.get("id") not in new_set:
                    return "notes may only describe NEW record ids"
            for link in obj.get("links", []):
                if not isinstance(link, dict) or link.get("a") not in new_set | neigh_set or \
                        link.get("b") not in new_set | neigh_set or link.get("kind") not in LINK_KINDS:
                    return f"links need shown ids and a kind from {list(LINK_KINDS)}"
            for u in obj.get("updates", []):
                if not isinstance(u, dict) or u.get("id") not in neigh_set:
                    return "updates may only change EXISTING record ids"
            return None

        listing = "NEW RECORDS\n" + "\n".join(f"[{r}] (speaker {self.s.record(r).speaker_id}) {texts[r]}"  # type: ignore[union-attr]
                                              for r in new_ids)
        listing += "\n\nEXISTING RECORDS\n" + ("\n".join(f"[{r}] {texts[r]}" for r in neigh) or "(none)")
        obj = self.m.json_call("organise", self._prompt, listing, check, max_tokens=2500) if self.m.enabled else None
        if obj is None:
            obj = self._fallback(new_ids, texts)
        ops: list[Op] = []
        ev = {"turn": turn_id}
        if self.f.enrich:
            for n in obj.get("notes", []):
                ops.append(Op("annotate", {"record": n["id"], "keywords": _strs(n.get("keywords")),
                                           "tags": _strs(n.get("tags")), "context": str(n.get("context") or ""),
                                           "attributes": [a for a in n.get("attributes") or [] if isinstance(a, dict)],
                                           "sources": [x for x in neigh]}, "__organiser__", turn_id, "organised",
                              evidence=ev))
        if self.f.link:
            seen = set()
            for link in obj.get("links", []):
                a, b, kind = link["a"], link["b"], link["kind"]
                key = (min(a, b), max(a, b), kind)
                if a == b or key in seen or (a in neigh_set and b in neigh_set):
                    continue
                seen.add(key)
                ops.append(Op("link", {"a": a, "b": b, "kind": kind}, "__organiser__", turn_id, "organised",
                              evidence=ev, confidence=0.8))
        if self.f.evolve_neighbours:
            for u in obj.get("updates", [])[:3]:
                old = self.s.active_note(u["id"]) or {}
                ops.append(Op("annotate", {"record": u["id"],
                                           "keywords": _strs(u.get("keywords")) or old.get("keywords", []),
                                           "tags": _strs(u.get("tags")) or old.get("tags", []),
                                           "context": str(u.get("context") or old.get("context") or ""),
                                           "sources": list(new_ids)}, "__organiser__", turn_id, "organised",
                              evidence=ev, confidence=0.7))
        if not ops:
            return
        results = self.core.apply(ops, seq)
        touched = sorted({r.op.args["record"] for r in results if r.ok and r.op.op == "annotate"})
        self._reembed(touched)

    def _fallback(self, new_ids: list[str], texts: dict[str, str]) -> dict:
        notes = []
        for r in new_ids:
            toks = [t for t in tokens(texts[r]) if len(t) > 3][:6]
            notes.append({"id": r, "keywords": toks, "tags": [], "context": "", "attributes": []})
        self.s.bump("organise.fallback")
        return {"notes": notes, "links": [], "updates": []}

    def _reembed(self, rids: list[str]) -> None:
        if not rids:
            return
        docs = []
        for r in rids:
            note = self.s.active_note(r) or {}
            docs.append(" ".join([self.s.text_owner(r) or "", note.get("context", ""), " ".join(note.get("keywords", []))]))
        for r, v in zip(rids, self.e.encode(docs)):
            self.s.set_embedding(r, v, int((self.s.active_note(r) or {}).get("version", 0)))

    # ================================================================ consolidation (§8.7)
    def consolidate(self, turn_id: str, seq: int) -> dict[str, int]:
        self._since_consolidation = 0
        stats = {"promoted": 0, "retracted": 0, "kept_pending": 0, "clusters": 0}
        with self.s.atomic():
            for row in self.s.q("SELECT l.id, l.a, l.b, l.reorg_id FROM links l WHERE l.status='pending' "
                                "AND l.kind='same_value_slot'"):
                outcome = self._decide_slot_link(row["a"], row["b"], turn_id, seq)
                if outcome == "promote":
                    self.s.x("UPDATE links SET status='active' WHERE id=?", (row["id"],))
                    self.s.set_reorg_status(row["reorg_id"], "active", turn_id, "confirmed")
                    stats["promoted"] += 1
                elif outcome == "retract":
                    self.s.x("UPDATE links SET status='retracted' WHERE id=?", (row["id"],))
                    self.s.set_reorg_status(row["reorg_id"], "retracted", turn_id, "not confirmed")
                    stats["retracted"] += 1
                else:
                    stats["kept_pending"] += 1
            for row in self.s.q("SELECT slot, COUNT(*) n FROM records WHERE slot IS NOT NULL AND tombstoned_at IS NULL "
                                "AND superseded_by IS NULL GROUP BY slot HAVING n > 1"):
                self.s.signal(turn_id, "slot_multiple_current", {"slot_hash": self.s.fingerprint(row["slot"]),
                                                                 "n": row["n"]})
            if self.f.cluster:
                stats["clusters"] = self._recluster(turn_id)
            self.s.x("UPDATE _signals SET resolved=1 WHERE resolved=0 AND kind LIKE 'read_%'")
        for k, v in stats.items():
            self.s.bump(f"consolidate.{k}", v)
        return stats

    def _decide_slot_link(self, a: str, b: str, turn_id: str, seq: int) -> str:
        ra, rb = self.s.record(a), self.s.record(b)
        if ra is None or rb is None or ra.tombstoned_at or rb.tombstoned_at:
            return "retract"
        old, new = (ra, rb) if (ra.created_seq, ra.id) < (rb.created_seq, rb.id) else (rb, ra)
        if old.superseded_by == new.id:
            return "promote"
        if old.superseded_by or new.superseded_by:
            return "retract"
        if self.s.q("SELECT 1 FROM links WHERE status='active' AND kind='contradicts' AND "
                    "((a=? AND b=?) OR (a=? AND b=?))", (a, b, b, a)):
            return "retract"
        if self.s.view_of(old.id, new.speaker_id) != "full":
            return "pending"
        if set(self.s.subjects(old.id)) != set(self.s.subjects(new.id)) or not self.s.subjects(old.id):
            return "retract"
        embs = self.s.embeddings([old.id, new.id])
        if old.id not in embs or new.id not in embs:
            return "pending"
        sim = float(np.dot(embs[old.id], embs[new.id]))
        if sim < max(0.6, self.cfg.supersede_similarity):
            return "retract"
        res = self.core.apply([Op("supersede", {"old": old.id, "new": new.id}, new.speaker_id, turn_id, "organised",
                                  evidence={"link": [a, b]}, confidence=sim)], seq)
        return "promote" if res and res[0].ok else "retract"

    def _recluster(self, turn_id: str) -> int:
        alive = {r.id for r in self.s.records(current_only=False)}
        parent = {r: r for r in alive}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        marks = ",".join("?" * len(CLUSTER_KINDS))
        for row in self.s.q(f"SELECT a, b FROM links WHERE status='active' AND kind IN ({marks})", CLUSTER_KINDS):
            if row["a"] in parent and row["b"] in parent:
                ra, rb = find(row["a"]), find(row["b"])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
        groups: dict[str, list[str]] = {}
        for r in alive:
            groups.setdefault(find(r), []).append(r)
        self.s.x("DELETE FROM cluster_members")
        self.s.x("UPDATE clusters SET status='retracted' WHERE status='active'")
        n = 0
        for root, members in groups.items():
            if len(members) < 2:
                continue
            cid = "c" + root
            kws: dict[str, int] = {}
            for m in members:
                for k in (self.s.active_note(m) or {}).get("keywords", []):
                    kws[k] = kws.get(k, 0) + 1
            label = ", ".join(k for k, _ in sorted(kws.items(), key=lambda kv: -kv[1])[:5])
            keyring.new_key(self.s.conn, cid)
            gid = self.s.new_reorg("cluster", {"cluster": cid, "n": len(members)}, {"turn": turn_id}, 1.0,
                                   "active", turn_id)
            self.s.x("INSERT OR REPLACE INTO clusters(id, label, status, reorg_id) VALUES (?,?,?,?)",
                     (cid, keyring.seal(self.s.conn, cid, "label", label), "active", gid))
            for m in members:
                self.s.x("INSERT OR REPLACE INTO cluster_members(cluster_id, record_id, status, reorg_id) "
                         "VALUES (?,?,?,?)", (cid, m, "active", gid))
            n += 1
        return n


def _strs(x: object) -> list[str]:
    return [str(v)[:60] for v in x if isinstance(v, (str, int, float))] if isinstance(x, list) else []


def dump_ledger(store: Store) -> list[dict]:
    return [dict(r) | {"args": json.loads(r["args"] or "{}")} for r in store.q("SELECT * FROM _reorgs ORDER BY id")]
