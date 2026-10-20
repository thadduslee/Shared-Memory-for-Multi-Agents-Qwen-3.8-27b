from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from .a_mem import AMemAgent, AMemoryNode
from .base import Checkpoint, Turn
from bench.retrieval import EmbeddingRetriever


# Sensitive-content taxonomy shared by every domain. Standing scope rules ("rides only, no diagnosis or callback
# numbers") are stated in these terms, and each note is tagged in the same terms when it is written.
SENSITIVE_CATEGORIES = (
    "identifier", "contact", "diagnosis", "lab_result", "medication", "financial",
    "credential", "location", "schedule", "legal", "personal",
)
GOV_OPS = ("delete", "supersede", "restrict", "grant", "revoke")
DELETED_MASK = "[deleted]"
WITHHELD_MASK = "[withheld]"


@dataclass
class Facet:
    """One retrievable view of a note. A note starts as one full facet; deletions and restrictions split off
    masked facets. Each facet is a point in content space plus governance coordinates (see `_facet_matrix`)."""

    fid: str
    mem_id: str
    text: str
    valid_from: int
    valid_to: Optional[int] = None  # exclusive; None = still valid
    deny: Dict[str, float] = field(default_factory=dict)  # principal id or "role:<role>" -> confidence
    sensitive: Set[str] = field(default_factory=set)
    embedding: Optional[np.ndarray] = None


@dataclass
class GovNode(AMemoryNode):
    turn_num: int = 0
    addressee: str = ""
    sensitive: List[str] = field(default_factory=list)
    superseded_at: Optional[str] = None
    gov_subjects: List[str] = field(default_factory=list)  # principals/roles a governance op on this turn concerned


class _ReplyCache:
    """Append-only JSONL cache of write-time LLM replies keyed by prompt hash, so a query-side change (the
    gate threshold, rendering) can be rerun without paying for ingest again."""

    _lock = threading.Lock()

    def __init__(self, path: Optional[str]) -> None:
        self.path = Path(path) if path else None
        self._mem: Dict[str, str] = {}
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    self._mem[row["k"]] = row["v"]
                except Exception:
                    continue

    @staticmethod
    def key(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def get(self, k: str) -> Optional[str]:
        return self._mem.get(k)

    def put(self, k: str, v: str) -> None:
        self._mem[k] = v
        if self.path:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")


class AMemGovAgent(AMemAgent):
    """A-Mem with governance (bench agent `a_mem_gov`). Four changes over `a_mem`, each behind a flag:

    1. render_chrono: retrieved notes are shown in turn order with turn id, time, speaker role and addressee.
    2. asker_channel: up to `asker_k` slots of the `top_k` budget go to notes about the requester (their own
       turns, turns addressed to or naming them, turns that changed their access), whatever the question topic.
    3. evolve_gov: the per-turn metadata call also emits governance ops on earlier notes — delete (close the
       note's validity and split off a masked facet), supersede (mark stale), restrict / grant / revoke (deny
       entries on notes, or standing category scopes per principal).
    4. gate: governance is part of the vector space. A facet is [content ; a*sensitive ; a*deny ; a*validity]
       and a query is [content ; -a*disallowed(requester) ; -a*onehot(requester) ; a*onehot(as_of)], so one
       inner product is similarity + b^2 * valid - a^2 * penalty, with b = 10a so validity dominates any
       penalty. A facet is released only if that product clears b^2 - a^2 * tau - 1, i.e. it is valid at
       as_of (always required) and its penalty is at most ~tau. tau is the utility/exposure dial.
    """

    def __init__(
        self,
        *,
        render_chrono: bool = True,
        asker_channel: bool = True,
        asker_k: int = 6,
        evolve_gov: bool = True,
        gate: bool = True,
        gate_tau: float = 0.5,
        gate_alpha: float = 10.0,
        candidate_k: int = 6,
        context_turns: int = 4,
        write_cache_path: Optional[str] = None,
        write_thinking: bool = False,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("metadata_mode", "llm")
        super().__init__(**kwargs)
        if self.retrieval_backend != "embedding":
            raise ValueError("a_mem_gov needs retrieval_backend=embedding (the gate works on embedding vectors)")
        self.render_chrono = bool(render_chrono)
        self.asker_channel = bool(asker_channel)
        self.asker_k = max(0, int(asker_k))
        self.evolve_gov = bool(evolve_gov)
        self.gate = bool(gate)
        self.gate_tau = float(gate_tau)
        self.gate_alpha = float(gate_alpha)
        self.candidate_k = max(0, int(candidate_k))
        self.context_turns = max(0, int(context_turns))
        self.write_thinking = bool(write_thinking)
        self._write_cache = _ReplyCache(write_cache_path)
        self._reset_gov_state()

    # -------------------- state --------------------

    def _reset_gov_state(self) -> None:
        self._facets: List[Facet] = []
        self._facets_by_mem: Dict[str, List[Facet]] = {}
        self._scopes: Dict[str, Dict[str, float]] = {}  # principal or "role:x" -> category -> disallow confidence
        self._turn_log: List[Turn] = []
        self._principals: Dict[str, Dict[str, str]] = {}
        self._roles: List[str] = []
        self._gov_log: List[Dict[str, Any]] = []
        self._tombstones: List[str] = []  # deleted values and their distinctive atoms; masked in later notes too

    def reset(self, episode: Dict[str, Any]) -> None:
        super().reset(episode)
        self._reset_gov_state()
        for p in ((episode.get("entities") or {}).get("principals") or []):
            pid = str(p.get("principal_id") or "")
            if pid:
                self._principals[pid] = {"role": str(p.get("role") or ""), "name": str(p.get("display_name") or "")}
        self._roles = sorted({v["role"] for v in self._principals.values() if v["role"]})

    # -------------------- ingest --------------------

    def ingest(self, turn: Turn) -> None:
        # Candidates for evolution are found from the raw turn before the note exists, so the write call can
        # name the earlier notes its ops act on.
        candidates = self._evolution_candidates(turn)
        meta = self._write_call(turn, candidates)
        node = GovNode(
            mem_id=f"mem_{turn.turn_id}",
            turn_id=turn.turn_id,
            principal_id=turn.speaker_principal_id,
            role=turn.speaker_role,
            timestamp=turn.timestamp,
            text=turn.text,
            record_refs=list(turn.record_refs or []),
            memory_ops=list(turn.memory_ops or []),
            summary=str(meta.get("summary") or "").strip(),
            keywords=self._norm_str_list(meta.get("keywords")),
            entities=self._norm_str_list(meta.get("entities")),
            categories=self._norm_str_list(meta.get("categories")),
            turn_num=self._turn_num(turn.turn_id),
            addressee=self._addressee(meta.get("addressee"), turn),
            sensitive=[c for c in self._norm_str_list(meta.get("sensitive")) if c in SENSITIVE_CATEGORIES],
        )
        self._nodes.append(node)
        self._nodes_by_id[node.mem_id] = node
        self._adj.setdefault(node.mem_id, [])
        self._turn_log.append(turn)

        index_meta = {
            "turn_id": node.turn_id, "principal_id": node.principal_id, "role": node.role,
            "record_refs": list(node.record_refs), "timestamp": node.timestamp, "summary": node.summary,
            "keywords": list(node.keywords), "entities": list(node.entities), "categories": list(node.categories),
        }
        usage = self.retriever.add(node.mem_id, node.retrieval_text(), index_meta)
        self._accum_usage(self._embed_usage_accum, usage)
        emb = self.retriever._emb[-1].copy()  # the vector just added for this note
        text = node.text
        if self.evolve_gov and self._tombstones:
            # a value deleted earlier and repeated now (often by someone probing for it) stays deleted
            masked = self._mask(text, self._tombstones, DELETED_MASK)
            if masked != text:
                text, emb = masked, self._embed(masked)
        self._add_facet(Facet(fid=f"{node.mem_id}#0", mem_id=node.mem_id, text=text, valid_from=node.turn_num,
                              sensitive=set(node.sensitive), embedding=emb))
        self._evolve_links(node)
        if self.evolve_gov:
            self._apply_ops(node, meta.get("ops") or [])

    def _evolution_candidates(self, turn: Turn) -> List[GovNode]:
        if not self._nodes or self.candidate_k <= 0:
            return []
        hits, _, _ = self.retriever.search(turn.text, top_k=self.candidate_k)
        picked = [self._nodes_by_id[h.chunk_id] for h in hits if h.chunk_id in self._nodes_by_id]
        for n in self._nodes[-3:]:
            if n not in picked:
                picked.append(n)
        return sorted(picked, key=lambda n: n.turn_num)  # type: ignore[attr-defined]

    def _write_call(self, turn: Turn, candidates: List[GovNode]) -> Dict[str, Any]:
        if self.llm_router is None:
            return self._analyze_turn_metadata_heuristic(turn)
        prompt = self._write_prompt(turn, candidates)
        k = _ReplyCache.key(prompt)
        text = self._write_cache.get(k)
        if text is None:
            opts = None if self.write_thinking else {"chat_template_kwargs": {"enable_thinking": False}}
            try:
                res = self.llm_router.complete_result(system_prompt="", user_prompt=prompt, provider_options=opts)
            except Exception as e:
                self.logger.warning("a_mem_gov write call failed for %s: %s", turn.turn_id, e)
                return self._analyze_turn_metadata_heuristic(turn)
            self._accum_usage(self._meta_llm_usage_accum, getattr(res, "usage", {}) or {})
            text = getattr(res, "text", "") or ""
            if isinstance(self._parse_json_loose(text), dict):
                self._write_cache.put(k, text)
        parsed = self._parse_json_loose(text)
        if not isinstance(parsed, dict):
            self.logger.warning("a_mem_gov write parse failed for %s; heuristic metadata, no ops", turn.turn_id)
            return self._analyze_turn_metadata_heuristic(turn)
        return parsed

    def _write_prompt(self, turn: Turn, candidates: List[GovNode]) -> str:
        people = "\n".join(f"- {pid} | role={v['role']} | {v['name']}" for pid, v in self._principals.items())
        scopes = self._describe_scopes() or "(none)"
        recent = "\n".join(f"{t.turn_id} {t.speaker_principal_id}: {t.text}"
                           for t in self._turn_log[-self.context_turns:]) or "(none)"
        cands = "\n".join(f"[{n.mem_id}] {n.turn_id} {n.principal_id}: {self._current_text(n)}"
                          for n in candidates) or "(none)"
        return (
            f"You maintain the shared memory of a multi-party {self._domain_label} assistant. A new turn arrives. "
            "Write its memory note and say whether it changes earlier memory or who may see it.\n\n"
            f"PEOPLE (principal_id | role | name):\n{people}\n\n"
            f"STANDING ACCESS RULES SO FAR:\n{scopes}\n\n"
            f"RECENT TURNS (context only):\n{recent}\n\n"
            f"EARLIER NOTES THIS TURN MAY ACT ON:\n{cands}\n\n"
            f"NEW TURN {turn.turn_id} from {turn.speaker_principal_id} ({turn.speaker_role}):\n{turn.text}\n\n"
            "Return STRICT JSON only, with keys:\n"
            "- summary: <=30 words; keywords: [strings]; entities: [principal_ids or names]; categories: [short tags]\n"
            "- addressee: principal_id this turn is said to, or null\n"
            f"- sensitive: the categories of sensitive content this turn contains or names (a question that names a "
            f"diagnosis, value or contact counts), from {list(SENSITIVE_CATEGORIES)}\n"
            "- ops: list of changes this turn makes to memory or access. Usually []. Each op is an object:\n"
            '  {"op": "delete", "values": [exact strings to erase], "notes": [mem_ids], "confidence": 0-1}\n'
            "     the owner or an authorised person asks to delete/remove/forget information, or it is confirmed\n"
            "     deleted. Copy each value VERBATIM from the notes or the turn (numbers, codes, names, phrases).\n"
            '  {"op": "supersede", "notes": [mem_ids], "confidence": 0-1}\n'
            "     this turn replaces values in those notes with new ones (an update, correction, reschedule).\n"
            '  {"op": "restrict", "principals": [principal_ids or "role:<role>"], "categories": [sensitive categories],\n'
            '   "values": [exact strings], "notes": [mem_ids], "confidence": 0-1}\n'
            "     someone with authority limits what those principals may see. Use categories for a standing\n"
            "     rule (applies to future notes too); use values/notes for specific information.\n"
            '  {"op": "grant", ...same keys as restrict}  consent or assignment that gives those principals access.\n'
            '  {"op": "revoke", ...same keys as restrict}  earlier consent or access is withdrawn.\n'
            "Only emit an op when this turn itself requests, performs, or confirms the change. Requests that are "
            "refused or not authorised make no op. Questions make no op.\n"
        )

    def _addressee(self, raw: Any, turn: Turn) -> str:
        a = str(raw or "").strip()
        if a in self._principals and a != turn.speaker_principal_id:
            return a
        if self._turn_log and self._turn_log[-1].speaker_principal_id != turn.speaker_principal_id:
            return self._turn_log[-1].speaker_principal_id
        return ""

    # -------------------- governance ops (change 3) --------------------

    def _apply_ops(self, node: GovNode, ops: List[Any]) -> None:
        t = node.turn_num
        for op in ops:
            if not isinstance(op, dict) or str(op.get("op")) not in GOV_OPS:
                continue
            kind = str(op["op"])
            conf = self._conf(op.get("confidence"))
            notes = [m for m in self._norm_str_list(op.get("notes")) if m in self._nodes_by_id and m != node.mem_id]
            values = [v for v in self._norm_str_list(op.get("values")) if len(v) >= 3]
            subjects = self._subjects(op.get("principals"))
            cats = [c for c in self._norm_str_list(op.get("categories")) if c in SENSITIVE_CATEGORIES]
            applied = False
            if kind == "delete" and conf >= 0.5 and (values or notes):
                applied = self._delete(t, values, notes, node)
            elif kind == "supersede" and conf >= 0.5 and notes:
                for m in notes:
                    old = self._nodes_by_id[m]
                    if isinstance(old, GovNode) and old.superseded_at is None:
                        old.superseded_at = node.turn_id
                applied = True
            elif kind in ("restrict", "revoke") and subjects:
                for s in subjects:
                    for c in cats:
                        self._scopes.setdefault(s, {})[c] = max(conf, self._scopes.get(s, {}).get(c, 0.0))
                if values or notes:
                    self._restrict(values, notes, subjects, conf)
                applied = bool(cats or values or notes)
            elif kind == "grant" and subjects:
                for s in subjects:
                    for c in (cats or list(self._scopes.get(s, {}))):
                        self._scopes.get(s, {}).pop(c, None)
                    for m in notes:
                        for f in self._facets_by_mem.get(m, []):
                            f.deny.pop(s, None)
                applied = True
            if applied:
                node.gov_subjects = self._dedup_keep_order(node.gov_subjects + subjects)
                self._gov_log.append({"turn_id": node.turn_id, "op": kind, "subjects": subjects, "categories": cats,
                                      "notes": notes, "n_values": len(values), "confidence": conf})

    def _delete(self, t: int, values: List[str], notes: List[str], src: GovNode) -> bool:
        people = {w for pid in self._principals for w in self._name_tokens(pid)}
        atoms = [a for v in values for a in self._atoms(v) if not (set(a.lower().split()) & people)]
        values = self._dedup_keep_order(values + atoms)
        self._tombstones = self._dedup_keep_order(self._tombstones + values)
        targets = set(notes)
        if values:
            for n in self._nodes:
                if any(v.lower() in self._current_text(n).lower() for v in values):
                    targets.add(n.mem_id)
        changed = False
        for m in targets:
            for f in list(self._live_facets(m, t)):
                f.valid_to = t
                changed = True
                masked = self._mask(f.text, values, DELETED_MASK) if values else ""
                # A note named without values goes entirely; with values, the rest of it stays retrievable.
                if masked and masked != f.text and self._has_content(masked):
                    self._add_facet(Facet(fid=f"{m}#{len(self._facets_by_mem[m])}", mem_id=m, text=masked,
                                          valid_from=t, deny=dict(f.deny), sensitive=set(f.sensitive),
                                          embedding=self._embed(masked)))
        return changed

    def _restrict(self, values: List[str], notes: List[str], subjects: List[str], conf: float) -> None:
        targets = set(notes)
        if values:
            for n in self._nodes:
                if any(v.lower() in self._current_text(n).lower() for v in values):
                    targets.add(n.mem_id)
        now = self._nodes[-1].turn_num if self._nodes else 0  # type: ignore[attr-defined]
        for m in targets:
            for f in list(self._live_facets(m, now)):
                if any(f.deny.get(s, 0.0) >= conf for s in subjects):
                    continue
                masked = self._mask(f.text, values, WITHHELD_MASK) if values else ""
                for s in subjects:
                    f.deny[s] = max(conf, f.deny.get(s, 0.0))
                if masked and masked != f.text and self._has_content(masked):
                    # the masked view stays visible to the restricted principals
                    self._add_facet(Facet(fid=f"{m}#{len(self._facets_by_mem[m])}", mem_id=m, text=masked,
                                          valid_from=f.valid_from, valid_to=f.valid_to,
                                          deny={k: v for k, v in f.deny.items() if k not in subjects},
                                          sensitive=set(f.sensitive), embedding=self._embed(masked)))

    # -------------------- governance vector space (change 4) --------------------

    def _gov_axes(self) -> Tuple[List[str], int]:
        subjects = list(self._principals) + [f"role:{r}" for r in self._roles]
        horizon = max([self._turn_num(t.turn_id) for t in self._turn_log] + [0]) + 2
        return subjects, horizon

    def _facet_matrix(self, facets: List[Facet]) -> Tuple[np.ndarray, List[str], int]:
        """Rows are [content ; a*sensitive ; a*deny ; a*validity-thermometer]."""
        subjects, horizon = self._gov_axes()
        sidx = {s: i for i, s in enumerate(subjects)}
        a = self.gate_alpha
        E = np.stack([f.embedding for f in facets]).astype(np.float32)
        K = np.zeros((len(facets), len(SENSITIVE_CATEGORIES)), np.float32)
        M = np.zeros((len(facets), len(subjects)), np.float32)
        V = np.zeros((len(facets), horizon), np.float32)
        for i, f in enumerate(facets):
            for c in f.sensitive:
                K[i, SENSITIVE_CATEGORIES.index(c)] = 1.0
            for s, w in f.deny.items():
                if s in sidx:
                    M[i, sidx[s]] = w
            V[i, max(0, f.valid_from): (f.valid_to if f.valid_to is not None else horizon)] = 1.0
        b = self._validity_scale()
        return np.hstack([E.astype(np.float64), a * K, a * M, b * V]), subjects, horizon

    def _query_vector(self, qc: np.ndarray, asker: str, role: str, as_of: int, subjects: List[str],
                      horizon: int) -> np.ndarray:
        """[content ; -a*disallowed categories ; -a*onehot(requester and role) ; a*onehot(as_of)]."""
        a = self.gate_alpha
        d = np.zeros(len(SENSITIVE_CATEGORIES), np.float32)
        for key in (asker, f"role:{role}"):
            for c, w in self._scopes.get(key, {}).items():
                d[SENSITIVE_CATEGORIES.index(c)] = max(d[SENSITIVE_CATEGORIES.index(c)], w)
        e = np.zeros(len(subjects), np.float32)
        for key in (asker, f"role:{role}"):
            if key in subjects:
                e[subjects.index(key)] = 1.0
        t = np.zeros(horizon, np.float32)
        t[min(max(as_of, 0), horizon - 1)] = 1.0
        b = self._validity_scale()
        return np.concatenate([qc.astype(np.float64), -a * d, -a * e, b * t])

    def _validity_scale(self) -> float:
        # Validity must stay a hard constraint for every tau: b^2 = 100 a^2 exceeds any reachable penalty
        # budget (a^2 * tau with tau < ~99), so a facet that is not valid at as_of never clears the threshold.
        return 10.0 * self.gate_alpha

    def _gated_scores(self, checkpoint: Checkpoint, qc: np.ndarray) -> Tuple[Dict[str, Tuple[Facet, float]], Dict[str, Any]]:
        """Best released facet per note -> (facet, content similarity). One inner product per facet decides
        release; ranking among released facets uses the content part only."""
        facets = self._facets
        if not facets:
            return {}, {"n_facets": 0, "n_released": 0}
        X, subjects, horizon = self._facet_matrix(facets)
        q = self._query_vector(qc, checkpoint.asker_principal_id, checkpoint.asker_role,
                               self._turn_num(checkpoint.as_of_turn_id), subjects, horizon)
        scores = X @ q
        d = qc.shape[0]
        sims = X[:, :d] @ qc
        a2, b2 = self.gate_alpha ** 2, self._validity_scale() ** 2
        threshold = b2 - a2 * self.gate_tau - 1.0
        best: Dict[str, Tuple[Facet, float]] = {}
        for i, f in enumerate(facets):
            if self.gate:
                if scores[i] < threshold:
                    continue
            elif not (f.valid_to is None or f.valid_to > self._turn_num(checkpoint.as_of_turn_id)):
                continue  # without the gate, a closed facet is simply the old version of a live one
            if f.mem_id not in best or sims[i] > best[f.mem_id][1]:
                best[f.mem_id] = (f, float(sims[i]))
        return best, {"n_facets": len(facets), "n_released": len(best), "threshold": threshold}

    # -------------------- query --------------------

    def query(self, checkpoint: Checkpoint) -> Dict[str, Any]:
        qc, q_usage = self._embed_router.embed_query(checkpoint.query_text)
        released, gate_stats = self._gated_scores(checkpoint, np.asarray(qc, np.float32))

        n_asker = self.asker_k if self.asker_channel else 0
        asker_ids = self._asker_notes(checkpoint, released)[:n_asker]
        topic = self._topic_hits(checkpoint, released, exclude=set(asker_ids))
        chosen = list(asker_ids) + [h.chunk_id for h in topic][: max(0, self.top_k - len(asker_ids))]
        chosen = self._dedup_keep_order(chosen)

        if self.render_chrono:
            chosen.sort(key=lambda m: self._nodes_by_id[m].turn_num)  # type: ignore[attr-defined]
        else:
            rank = {h.chunk_id: i for i, h in enumerate(topic)}
            chosen.sort(key=lambda m: rank.get(m, -1))

        retrieved_memory: List[Dict[str, Any]] = []
        for m in chosen:
            node = self._nodes_by_id[m]
            facet, sim = released[m]
            retrieved_memory.append({
                "record_id": m, "turn_id": node.turn_id, "principal_id": node.principal_id, "role": node.role,
                "text": facet.text, "record_refs": list(node.record_refs), "score": sim,
                "facet": facet.fid, "asker_channel": m in asker_ids,
            })
        block = self._render(retrieved_memory) if self.render_chrono else None
        out = self._run_llm(checkpoint=checkpoint, retrieved_memory=retrieved_memory,
                            rendered_memory_block_override=block)
        out["retrieval_s"] = 0.0
        out["amem_gov"] = {**gate_stats, "n_asker": len(asker_ids), "tau": self.gate_tau,
                           "gov_ops_so_far": len(self._gov_log), "scopes": self._describe_scopes()}
        out["embedding_query_usage"] = q_usage
        out["amem_metadata_llm_usage_accum"] = dict(self._meta_llm_usage_accum)
        return out

    def _topic_hits(self, checkpoint: Checkpoint, released: Dict[str, Tuple[Facet, float]],
                    exclude: Set[str]) -> List[Any]:
        class _Hit:
            def __init__(self, chunk_id: str, score: float, metadata: Dict[str, Any]) -> None:
                self.chunk_id, self.score, self.metadata, self.text = chunk_id, score, metadata, ""

        def hit(m: str, score: float) -> Any:
            n = self._nodes_by_id[m]
            return _Hit(m, score, {"turn_id": n.turn_id, "principal_id": n.principal_id, "role": n.role,
                                   "entities": n.entities})

        ranked = sorted(((m, s) for m, (_, s) in released.items() if s > 0), key=lambda x: -x[1])
        hits = [hit(m, s) for m, s in ranked[: self.top_k]]
        # graph expansion over released notes only (A-Mem link walk)
        seen = {h.chunk_id for h in hits}
        for h in list(hits):
            for nid in self._adj.get(h.chunk_id, [])[: self.graph_expand_per_hit] if self.graph_expand_hops else []:
                if nid in released and nid not in seen:
                    hits.append(hit(nid, h.score * 0.85))
                    seen.add(nid)
        return [h for h in self._rerank_hits(checkpoint, hits) if h.chunk_id not in exclude]

    def _asker_notes(self, checkpoint: Checkpoint, released: Dict[str, Tuple[Facet, float]]) -> List[str]:
        """Notes about the requester, most recent first: what they said, what was said to them or about them,
        and turns that changed their access. Topic-independent, which is the point."""
        asker = checkpoint.asker_principal_id
        role_key = f"role:{checkpoint.asker_role}"
        names = self._name_tokens(asker)
        out: List[str] = []
        for n in reversed(self._nodes):
            if n.mem_id not in released:
                continue
            g: GovNode = n  # type: ignore[assignment]
            text = released[n.mem_id][0].text.lower()
            if (g.principal_id == asker or g.addressee == asker or asker in g.gov_subjects
                    or role_key in g.gov_subjects or any(re.search(rf"\b{re.escape(t)}\b", text) for t in names)):
                out.append(n.mem_id)
        return out

    def _render(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "(none)"
        lines = [
            "Memory is listed in conversation order (oldest first). Each line: turn, time, speaker (role) -> who it "
            "was said to.",
        ]
        if self.evolve_gov:
            lines.append(f"{DELETED_MASK} marks information erased on request; {WITHHELD_MASK} marks information "
                         "this requester was restricted from; '(superseded at tN)' marks a value later replaced.")
        lines.append("")
        for i, it in enumerate(items, 1):
            n: GovNode = self._nodes_by_id[it["record_id"]]  # type: ignore[assignment]
            to = f" -> {n.addressee}" if n.addressee else ""
            ts = f" {n.timestamp}" if n.timestamp else ""
            stale = f" (superseded at {n.superseded_at})" if (self.evolve_gov and n.superseded_at) else ""
            text = (it.get("text") or "").strip().replace("\n", " ")
            lines.append(f"Memory {i} [{n.turn_id}{ts}] {n.principal_id} ({n.role}){to}{stale}: {text}")
        return "\n".join(lines)

    # -------------------- helpers --------------------

    def _add_facet(self, f: Facet) -> None:
        self._facets.append(f)
        self._facets_by_mem.setdefault(f.mem_id, []).append(f)

    def _live_facets(self, mem_id: str, t: int) -> List[Facet]:
        return [f for f in self._facets_by_mem.get(mem_id, []) if f.valid_to is None or f.valid_to > t]

    def _current_text(self, n: AMemoryNode) -> str:
        live = [f for f in self._facets_by_mem.get(n.mem_id, []) if f.valid_to is None]
        return live[-1].text if live else n.text

    def _embed(self, text: str) -> np.ndarray:
        emb, usage = self._embed_router.embed_texts([text])
        self._accum_usage(self._embed_usage_accum, usage)
        return np.asarray(emb, np.float32).reshape(-1)

    @staticmethod
    def _mask(text: str, values: List[str], token: str) -> str:
        out = text
        for v in sorted(values, key=len, reverse=True):
            out = re.sub(re.escape(v), token, out, flags=re.IGNORECASE)
        return out

    @staticmethod
    def _atoms(value: str) -> List[str]:
        """Distinctive pieces of a deleted value that identify it on their own: tokens with digits (numbers,
        codes) and runs of capitalised words (names, places), minus the run at the start of the value."""
        atoms = re.findall(r"[A-Za-z]*\d[\w\-/.:]*\d|\d", value)
        atoms = [a for a in atoms if len(re.sub(r"\D", "", a)) >= 3]
        for m in re.finditer(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*", value):
            if m.start() > 0 or " " in m.group(0):
                atoms.append(m.group(0))
        return [a for a in atoms if len(a) >= 4]

    @staticmethod
    def _has_content(text: str) -> bool:
        rest = text.replace(DELETED_MASK, " ").replace(WITHHELD_MASK, " ")
        return len(re.findall(r"[A-Za-z]{3,}", rest)) >= 3

    @staticmethod
    def _conf(raw: Any) -> float:
        try:
            return min(1.0, max(0.0, float(raw)))
        except Exception:
            return 0.8

    def _subjects(self, raw: Any) -> List[str]:
        out = []
        for s in self._norm_str_list(raw):
            if s in self._principals:
                out.append(s)
            elif s.startswith("role:") and s[5:] in self._roles:
                out.append(s)
            elif s in self._roles:
                out.append(f"role:{s}")
        return self._dedup_keep_order(out)

    def _name_tokens(self, pid: str) -> List[str]:
        name = re.sub(r"\(.*?\)", "", self._principals.get(pid, {}).get("name", "")).strip().lower()
        toks = [name] if name else []
        toks += [w for w in re.split(r"[\s.]+", name) if len(w) >= 4 and w not in {"nurse", "front", "desk"}]
        return self._dedup_keep_order(toks + [pid.lower()])

    def _describe_scopes(self) -> str:
        rows = []
        for s, cats in self._scopes.items():
            live = sorted(c for c, w in cats.items() if w > 0)
            if live:
                rows.append(f"- {s} may NOT see: {', '.join(live)}")
        return "\n".join(rows)
