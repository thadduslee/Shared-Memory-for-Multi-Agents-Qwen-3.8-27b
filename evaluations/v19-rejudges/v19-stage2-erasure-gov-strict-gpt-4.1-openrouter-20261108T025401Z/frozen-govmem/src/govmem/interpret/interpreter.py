"""The interpreter (SPEC §7): one turn -> label (model) -> proposals (trusted code) -> core.

The model reports what the message says; this code turns that into operations from the closed vocabulary and
the core authorises them. With the model disabled or failing, a deterministic fallback keeps the memory
working and privacy-conservative.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from govmem.config import Settings
from govmem.core.ops import Core, Op, OpResult
from govmem.core.store import Store
from govmem.embed import Embedder
from govmem.interpret.roster import Roster
from govmem.llm import MemoryModel
from govmem.retrieve.relevance import Relevance
from govmem.text import is_verbatim, mask, slugify, values

PROMPTS = Path(__file__).parent / "prompts"
KINDS = ("fact", "instruction", "correction", "deletion", "question", "chatter")
STORE_KINDS = ("fact", "instruction", "correction")


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


@dataclass
class Turn:
    turn_id: str
    timestamp: str
    speaker_id: str
    text: str
    kind_hint: str = ""


@dataclass
class TurnOutcome:
    turn_id: str
    kind: str
    new_records: list[str] = field(default_factory=list)
    results: list[OpResult] = field(default_factory=list)
    fallback: bool = False
    label: dict[str, Any] = field(default_factory=dict)


class Interpreter:
    def __init__(self, store: Store, core: Core, roster: Roster, model: MemoryModel, settings: Settings,
                 embedder: Embedder, relevance: Relevance) -> None:
        self.s, self.core, self.roster, self.m, self.cfg = store, core, roster, model, settings
        self.f = settings.features
        self.e = embedder
        self.rel = relevance
        self.recent: deque[tuple[str, str]] = deque(maxlen=3)
        self.window: deque[set[str]] = deque(maxlen=max(1, settings.roster_window))
        self.instructions: deque[tuple[str, str]] = deque(maxlen=12)

    # ================================================================ entry point
    def ingest(self, turn: Turn) -> TurnOutcome:
        seq = self.s.register_turn(turn.turn_id, turn.timestamp, turn.speaker_id)
        if self.cfg.roster_window:
            self.window.append({turn.speaker_id, *self.roster.mentioned_ids(turn.text)})
            self.roster.set_active(set().union(*self.window))
        label = self._label(turn) if (self.m.enabled and self.f.model_interpretation) else None
        fallback = label is None
        if fallback:
            label = self._fallback_label(turn)
            self.s.bump("label.fallback")
        out = TurnOutcome(turn.turn_id, label["kind"], fallback=fallback, label=label)
        with self.s.atomic():
            removed_values: list[str] = []
            if (label.get("remove") or label["kind"] == "deletion") and self.f.model_deletion:
                removed_values = self._handle_removal(turn, seq, label, out)
            if label.get("rescope") and self.f.rescoping:
                self._handle_rescope(turn, seq, label, out)
            if label["kind"] in STORE_KINDS or (label["kind"] == "deletion" and label["quotes"]):
                self._handle_store(turn, seq, label, out, removed_values)
            self._handle_aliases(turn, seq, label, out)
        for rid in out.new_records:
            text = self.s.text_owner(rid) or ""
            self.s.set_embedding(rid, self.e.encode([text])[0], 0)
        self._remember(turn, label)
        return out

    # ================================================================ label
    def _context(self, turn: Turn) -> str:
        speaker_role = self.s.role_of(turn.speaker_id)
        slots = sorted({r["slot"] for r in self.s.q(
            "SELECT DISTINCT r.slot FROM records r JOIN record_views v ON v.record_id=r.id "
            "WHERE v.principal_id=? AND v.view='full' AND r.slot IS NOT NULL AND r.tombstoned_at IS NULL "
            "AND r.superseded_by IS NULL", (turn.speaker_id,))})[-40:]
        hints = [f"{m.alias} -> {m.target}" for m in self.roster.mentions(turn.text)] if self.f.reference_resolution else []
        parts = [
            "MENU\n" + self.roster.render(),
            "STANDING INSTRUCTIONS (recent, oldest first)\n" +
            ("\n".join(f"- {sp}: {tx}" for sp, tx in self.instructions) or "(none)"),
            "PREVIOUS MESSAGES\n" + ("\n".join(f"- {sp}: {tx}" for sp, tx in self.recent) or "(none)"),
            "READABLE SLOTS\n" + (", ".join(slots) or "(none)"),
            "NAME HINTS\n" + ("; ".join(dict.fromkeys(hints)) or "(none)"),
            f"MESSAGE\nspeaker: {turn.speaker_id} ({speaker_role})\ntime: {turn.timestamp}\n"
            + (f"channel: {turn.kind_hint}\n" if turn.kind_hint else "") + f"text: {turn.text}",
        ]
        return "\n\n".join(parts)

    def _validate_label(self, obj: dict) -> str | None:
        if obj.get("kind") not in KINDS:
            return f"kind must be one of {list(KINDS)}"
        if not isinstance(obj.get("quotes", []), list):
            return "quotes must be a list"
        for q in obj.get("quotes") or []:
            if not isinstance(q, dict) or not isinstance(q.get("text"), str):
                return "each quote must be an object with a text string"
        aud = obj.get("audience", {})
        if not isinstance(aud, dict):
            return "audience must be an object with full and redacted lists"
        for key in ("exclude", "protected_values", "aliases"):
            if not isinstance(obj.get(key, []) or [], list):
                return f"{key} must be a list"
        if obj.get("rescope") is not None and not isinstance(obj.get("rescope"), dict):
            return "rescope must be an object or null"
        if obj.get("remove") is not None and not isinstance(obj.get("remove"), dict):
            return "remove must be an object or null"
        return None

    def _label(self, turn: Turn) -> dict | None:
        obj = self.m.json_call("label", _prompt("label"), self._context(turn), self._validate_label, max_tokens=2048)
        if obj is None:
            return None
        obj.setdefault("quotes", [])
        obj["audience"] = obj.get("audience") or {}
        return obj

    def _fallback_label(self, turn: Turn) -> dict:
        text = turn.text.strip()
        kind = "question" if text.endswith("?") else "fact"
        about = self.roster.mentioned_ids(text) if self.f.reference_resolution else []
        return {"kind": kind, "quotes": [{"text": text, "about": about, "owners": [], "slot": None, "shareable": None}],
                "audience": {"full": ["speaker", "about"], "redacted": []}, "exclude": [],
                "protected_values": values(text), "rescope": None, "remove": None, "aliases": []}

    # ================================================================ store new information
    def _handle_store(self, turn: Turn, seq: int, label: dict, out: TurnOutcome, removed_values: list[str]) -> None:
        speaker = turn.speaker_id
        known = set(self.roster.principals) | set(self.roster.entities)
        protected = [v for v in (label.get("protected_values") or []) if isinstance(v, str) and is_verbatim(v, turn.text)]
        self.s.bump("label.protected_dropped", len(label.get("protected_values") or []) - len(protected))
        quotes = []
        for q in label.get("quotes") or []:
            text = str(q.get("text") or "").strip()
            if not text:
                continue
            if not is_verbatim(text, turn.text):
                self.s.bump("label.quote_not_verbatim")
                continue
            if text.endswith("?"):
                self.s.bump("label.question_dropped")
                continue
            quotes.append(q)
        if self.f.completeness_guard:
            covered = " ".join(str(q["text"]) for q in quotes)
            missing = [v for v in values(turn.text) + protected if not is_verbatim(v, covered)] if quotes else ["*"]
            if missing and label["kind"] in STORE_KINDS:
                about = list(dict.fromkeys(a for q in quotes for a in (q.get("about") or []))) or \
                    self.roster.mentioned_ids(turn.text)
                owners = list(dict.fromkeys(a for q in quotes for a in (q.get("owners") or [])))
                quotes.append({"text": turn.text.strip(), "about": about, "owners": owners, "slot": None,
                               "shareable": None, "_whole": True})
                self.s.bump("label.residual_message_kept")
        aud = label.get("audience") or {}
        full_t, red_t = list(aud.get("full") or []), list(aud.get("redacted") or [])
        if self.f.privacy_floor and protected:
            if "everyone" in full_t:
                self.s.bump("label.privacy_floor_applied")
            full_t = [t for t in full_t if t != "everyone"]
        exclude = {e for e in (label.get("exclude") or []) if e in self.roster.principals}
        ops: list[Op] = []
        for q in quotes:
            about = {a for a in (q.get("about") or []) if a in known}
            owners = {o for o in (q.get("owners") or []) if o in self.roster.principals}
            full, explicit, unk1 = self.roster.resolve(full_t, speaker=speaker, about=about | owners)
            red, explicit2, unk2 = self.roster.resolve(red_t, speaker=speaker, about=about | owners)
            self.s.bump("label.unknown_tokens", unk1 + unk2)
            text = str(q["text"]).strip()
            forgotten = self.s.forgotten_in(values(text)) + self.s.forgotten_phrases_in(text)
            if forgotten:
                self.s.bump("label.forgotten_value_masked", len(forgotten))
            if removed_values or forgotten:
                text = mask(text, removed_values + forgotten)
            shareable = q.get("shareable") if isinstance(q.get("shareable"), str) else None
            if shareable and any(is_verbatim(v, shareable) for v in protected + removed_values):
                shareable = None
                self.s.bump("label.shareable_dropped")
            slot = slugify(q["slot"]) if isinstance(q.get("slot"), str) and q.get("slot") and self.f.supersession else None
            subjects = {a: (a in owners) for a in about} | {o: True for o in owners}
            ops.append(Op("add", {
                "text": text, "verbatim_of": None if (removed_values or forgotten) else turn.text, "kind": label["kind"],
                "slot": slot, "subjects": subjects, "audience_full": sorted(full),
                "audience_redacted": sorted(red) if shareable else [], "redacted_text": shareable,
                "exclude": sorted(exclude), "explicit": sorted(explicit | explicit2),
                "protected_values": [v for v in protected if is_verbatim(v, text)], "timestamp": turn.timestamp,
            }, speaker, turn.turn_id, "interpreted"))
        results = self.core.apply(ops, seq)
        out.results += results
        new_ids = [rid for r in results if r.ok for rid in r.record_ids]
        out.new_records += new_ids
        if self.f.supersession:
            self._supersede(turn, seq, new_ids, out)

    def _supersede(self, turn: Turn, seq: int, new_ids: list[str], out: TurnOutcome) -> None:
        ops = []
        for rid in new_ids:
            rec = self.s.record(rid)
            if rec is None or not rec.slot:
                continue
            olds = [r["id"] for r in self.s.q(
                "SELECT id FROM records WHERE slot=? AND id!=? AND tombstoned_at IS NULL AND superseded_by IS NULL "
                "AND source_turn != ?", (rec.slot, rid, turn.turn_id))]
            olds = [o for o in olds if self.s.view_of(o, turn.speaker_id) == "full"]
            if not olds:
                continue
            new_text = self.s.text_owner(rid) or ""
            old_texts = [self.s.text_owner(o) or "" for o in olds]
            vecs = self.e.encode([new_text] + old_texts)
            sims = vecs[1:] @ vecs[0]
            for o, old_text, sim in zip(olds, old_texts, sims):
                # A value replaces a value: both must hold one, unless the turn itself is a correction.
                if rec.kind != "correction" and not (values(new_text) and values(old_text)):
                    self.s.bump("supersede.skipped_no_value")
                    continue
                if float(sim) >= self.cfg.supersede_similarity:
                    ops.append(Op("supersede", {"old": o, "new": rid, "timestamp": turn.timestamp},
                                  turn.speaker_id, turn.turn_id, "interpreted", confidence=float(sim)))
                else:
                    self.s.bump("supersede.skipped_dissimilar")
                    self.s.signal(turn.turn_id, "slot_conflict", {"a": o, "b": rid})
        if ops:
            out.results += self.core.apply(ops, seq)

    # ================================================================ removal
    def _candidates(self, speaker: str, query: str, *, history: bool, k: int = 15) -> list[tuple[str, str]]:
        rids = self.s.records_visible_to(speaker, current_only=not history)
        ranked = self.rel.rank(query, rids, k)
        return [(rid, self.s.text_owner(rid) or "") for rid, _ in ranked]

    def _handle_removal(self, turn: Turn, seq: int, label: dict, out: TurnOutcome) -> list[str]:
        what = str((label.get("remove") or {}).get("what") or turn.text)
        cands = self._candidates(turn.speaker_id, what + " " + turn.text, history=self.f.history_deletion)
        if not cands:
            self.s.bump("remove.no_candidates")
            return []
        listing = "\n".join(f"[{rid}] {txt}" for rid, txt in cands)
        ids = {rid for rid, _ in cands}
        texts = dict(cands)

        def check(obj: dict) -> str | None:
            t = obj.get("targets")
            if not isinstance(t, list):
                return "targets must be a list"
            for x in t:
                if not isinstance(x, dict) or x.get("id") not in ids:
                    return "every target needs an id from the list"
            return None

        user = f"MESSAGE from {turn.speaker_id}: {turn.text}\n\nRECORDS\n{listing}"
        obj = self.m.json_call("remove", _prompt("remove"), user, check, max_tokens=800) if self.m.enabled else None
        if obj is None:
            self.s.bump("remove.fallback_none")
            return []
        parts, targets = {}, []
        for t in obj["targets"]:
            rid = t["id"]
            targets.append(rid)
            part = t.get("part")
            if isinstance(part, str) and part.strip() and is_verbatim(part, texts[rid]) and self.f.partial_delete:
                parts[rid] = part
        if not targets:
            return []
        removed_values: list[str] = []
        for rid in targets:
            removed_values += values(parts.get(rid) or texts[rid])
        res = self.core.apply([Op("delete", {"records": targets, "parts": parts}, turn.speaker_id, turn.turn_id,
                                  "interpreted")], seq)
        out.results += res
        return list(dict.fromkeys(removed_values))

    # ================================================================ rescoping
    def _handle_rescope(self, turn: Turn, seq: int, label: dict, out: TurnOutcome) -> None:
        rs = label["rescope"]
        speaker = turn.speaker_id
        share, _e1, _u1 = self.roster.resolve(list(rs.get("share_with") or []), speaker=speaker, about=set())
        withhold, _e2, _u2 = self.roster.resolve(list(rs.get("withhold_from") or []), speaker=speaker, about=set())
        share -= {speaker}
        withhold -= {speaker}
        view = rs.get("view") if rs.get("view") in ("full", "redacted") else "full"
        what = str(rs.get("what") or turn.text)
        if withhold and rs.get("all_about_speaker"):
            out.results += self.core.apply([Op("revoke", {"records": [], "principals": sorted(withhold),
                                                          "scope": speaker}, speaker, turn.turn_id)], seq)
        cands = self._candidates(speaker, what + " " + turn.text, history=False)
        if not cands:
            self.s.bump("rescope.no_candidates")
            return
        ids = {rid for rid, _ in cands}
        listing = "\n".join(f"[{rid}] {txt}" for rid, txt in cands)
        user = f"MESSAGE from {speaker}: {turn.text}\nCHANGE: {json.dumps(rs, ensure_ascii=False)}\n\nRECORDS\n{listing}"

        def check(obj: dict) -> str | None:
            v = obj.get("ids")
            if not isinstance(v, list) or any(x not in ids for x in v):
                return "ids must be a list of record ids from the list"
            return None

        obj = self.m.json_call("rescope", _prompt("rescope"), user, check, max_tokens=600)
        if obj is None:
            return
        chosen = list(dict.fromkeys(obj["ids"]))
        if withhold and chosen:
            out.results += self.core.apply([Op("revoke", {"records": chosen, "principals": sorted(withhold)},
                                               speaker, turn.turn_id)], seq)
        if share and chosen:
            agreed = chosen
            if self.f.widen_agreement:
                def check2(o: dict) -> str | None:
                    v = o.get("allowed")
                    if not isinstance(v, list) or any(x not in ids for x in v):
                        return "allowed must be a list of record ids from the list"
                    return None
                user2 = f"MESSAGE from {speaker}: {turn.text}\n\nRECORDS\n" + \
                    "\n".join(f"[{rid}] {txt}" for rid, txt in reversed(cands))
                o2 = self.m.json_call("rescope_check", _prompt("rescope_check"), user2, check2, max_tokens=600)
                second = set(o2["allowed"]) if o2 else set()
                agreed = [r for r in chosen if r in second]
                for r in chosen:
                    if r not in second:
                        self.s.new_reorg("grant", {"record": r, "principals": sorted(share), "view": view},
                                         {"turn": turn.turn_id}, 0.5, "pending", turn.turn_id, "no_agreement")
                        self.s.bump("rescope.widen_pending")
            if agreed:
                out.results += self.core.apply([Op("grant", {"records": agreed, "principals": sorted(share),
                                                             "view": view}, speaker, turn.turn_id)], seq)

    # ================================================================ aliases, memory of the turn
    def _handle_aliases(self, turn: Turn, seq: int, label: dict, out: TurnOutcome) -> None:
        ops = []
        for a in label.get("aliases") or []:
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a.get("id"):
                ops.append(Op("alias", {"alias": a["name"], "target": a["id"]}, turn.speaker_id, turn.turn_id,
                              "interpreted", evidence={"turn_text": turn.text}))
        if ops:
            out.results += self.core.apply(ops, seq)
            self.roster.refresh()

    def _remember(self, turn: Turn, label: dict) -> None:
        kind = label["kind"]
        shown = "[a request to remove earlier information]" if kind == "deletion" else turn.text
        self.recent.append((turn.speaker_id, shown[:600]))
        if kind == "instruction" or label.get("rescope"):
            self.instructions.append((turn.speaker_id, turn.text[:600]))


def embed_records(store: Store, embedder: Embedder, rids: list[str]) -> None:
    texts = [store.text_owner(r) or "" for r in rids]
    if not texts:
        return
    vecs = embedder.encode(texts)
    for rid, v in zip(rids, vecs):
        store.set_embedding(rid, np.asarray(v), 0)
