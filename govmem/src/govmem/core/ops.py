"""Operations (SPEC §4) and the authority model (SPEC §5). The only code that changes memory.

Proposals come from the interpreter, the organiser or a caller. Each is validated, authorised and applied
here, or rejected with a reason code. Tightening (restrict, revoke, delete) applies before anything else of
the same turn. A turn's accepted operations commit atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from govmem.config import Features
from govmem.core.store import VIEW_RANK, Store
from govmem.text import is_verbatim

TIGHTENING = ("restrict", "revoke", "delete")
VIEWS = ("full", "redacted", "existence")
LINK_KINDS = ("same_topic", "same_value_slot", "follow_up", "refers_to", "contradicts", "part_of")


@dataclass
class Op:
    op: str
    args: dict[str, Any]
    authority: str
    source_turn: str
    origin: str = "interpreted"          # declared | interpreted | organised | caller
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0


@dataclass
class OpResult:
    op: Op
    outcome: str                         # applied | rejected | pending
    reason: str = ""
    record_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "applied"


class Rejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Core:
    def __init__(self, store: Store, features: Features | None = None) -> None:
        self.s = store
        self.f = features or Features()

    # ---------------------------------------------------------------- authority helpers
    def subject_principals(self, rid: str) -> set[str]:
        pids = self.s.principal_ids()
        return {sid for sid in self.s.subjects(rid) if sid in pids}

    def can_grant(self, pid: str, rid: str) -> bool:
        """Owners and subject-principals may widen (SPEC §5.1 sources 2-3), and only if they can see it."""
        return (pid in self.s.owners(rid) or pid in self.subject_principals(rid)) and \
            self.s.view_of(rid, pid) == "full"

    def can_revise(self, pid: str, rid: str) -> bool:
        return self.s.view_of(rid, pid) == "full"

    def denied(self, pid: str, rid: str, seq: int, authority: str) -> bool:
        """§5.3: a widening for a denied principal stands only if newer than the denial AND by an owner/subject."""
        if not self.f.denial_ledger:
            return False
        targets = [rid, *self.s.subjects(rid).keys()]
        d = self.s.latest_denial(pid, targets)
        if d is None:
            return False
        rec = self.s.record(rid)
        owner_like = authority in self.s.owners(rid) or authority in self.subject_principals(rid)
        if rec is not None and authority == rec.speaker_id and authority in self.s.subjects(rid):
            owner_like = True
        return not (seq > d and owner_like)

    def relationship_access(self, subject_ids: list[str]) -> dict[str, str]:
        """Scope facts for the record's subjects: unqualified relationships and earlier share_scope grants."""
        out = self.s.scope_facts_for(subject_ids)
        return out

    # ---------------------------------------------------------------- apply
    def apply(self, ops: list[Op], seq: int) -> list[OpResult]:
        ordered = [o for o in ops if o.op in TIGHTENING] + [o for o in ops if o.op not in TIGHTENING]
        results: list[OpResult] = []
        with self.s.atomic():
            for op in ordered:
                handler = getattr(self, "_op_" + op.op, None)
                if handler is None:
                    res = OpResult(op, "rejected", "UNKNOWN_OPERATION")
                else:
                    try:
                        with self.s.atomic():
                            res = handler(op, seq)
                    except Rejected as exc:
                        res = OpResult(op, "rejected", exc.code)
                self.s.log(op.source_turn, op.op, _loggable(op.args), op.authority, op.origin, res.outcome, res.reason)
                self.s.bump(f"op.{op.op}.{res.outcome}")
                results.append(res)
        return results

    # ---------------------------------------------------------------- admit
    def _op_add(self, op: Op, seq: int) -> OpResult:
        a = op.args
        speaker = op.authority
        if speaker not in self.s.principal_ids():
            raise Rejected("UNKNOWN_PRINCIPAL")
        text = a["text"]
        if a.get("verbatim_of") is not None and not is_verbatim(text, a["verbatim_of"]):
            raise Rejected("NOT_VERBATIM")
        subjects: dict[str, bool] = dict(a.get("subjects") or {})
        rid = self.s.add_record(text=text, speaker_id=speaker, source_turn=op.source_turn, created_seq=seq,
                                created_at=a.get("timestamp", ""), kind=a.get("kind", "fact"), slot=a.get("slot"),
                                redacted_text=a.get("redacted_text"), derived_from=a.get("derived_from"))
        self.s.set_subjects(rid, subjects)
        self.s.add_protected(rid, list(a.get("protected_values") or []))
        explicit = set(a.get("explicit") or [])
        full = set(a.get("audience_full") or [])
        full |= {sid for sid, owner in subjects.items() if owner}
        full |= {sid for sid in subjects if sid in self.s.principal_ids()}
        if self.f.unqualified_relationship_access:
            for pid, view in self.relationship_access(list(subjects)).items():
                if view == "full":
                    full.add(pid)
        if self.f.open_access:
            full |= self.s.principal_ids()
        red = set(a.get("audience_redacted") or []) - full
        exclude = set(a.get("exclude") or [])
        known = self.s.principal_ids()
        for pid in sorted((full | red) & known):
            if pid == speaker:
                continue
            if pid in exclude:
                continue
            if self.f.denial_ledger and \
                    self.s.latest_denial(pid, [rid, *subjects.keys()]) is not None and \
                    (pid not in explicit or self.denied(pid, rid, seq, speaker)):
                self.s.bump("add.denial_blocked")
                continue
            self.s.set_view(rid, pid, "full" if pid in full else "redacted")
        self.s.set_view(rid, speaker, "full")       # speaking: always full (§5.1 source 1)
        for pid in exclude & known:
            if pid != speaker:
                self.s.set_view(rid, pid, "none")
                self.s.add_denial(pid, rid, seq, op.source_turn)
        return OpResult(op, "applied", record_ids=[rid])

    def _op_derive(self, op: Op, seq: int) -> OpResult:
        sources = list(op.args["sources"])
        if not sources or not all(self.s.view_of(r, op.authority) == "full" for r in sources):
            raise Rejected("AUTHORITY_CANNOT_SEE")
        aud = set.intersection(*(self.s.full_audience(r) for r in sources))
        rid = self.s.add_record(text=op.args["text"], speaker_id=op.authority, source_turn=op.source_turn,
                                created_seq=seq, created_at=op.args.get("timestamp", ""), kind="derived",
                                slot=None, derived_from=sources)
        subj: dict[str, bool] = {}
        for r in sources:
            subj.update(self.s.subjects(r))
        self.s.set_subjects(rid, subj)
        for pid in aud:
            self.s.set_view(rid, pid, "full")
        return OpResult(op, "applied", record_ids=[rid])

    # ---------------------------------------------------------------- scope
    def _op_grant(self, op: Op, seq: int) -> OpResult:
        view = op.args.get("view", "full")
        if view not in VIEWS:
            raise Rejected("BAD_VIEW")
        changed: list[str] = []
        for rid in op.args["records"]:
            rec = self.s.record(rid)
            if rec is None or rec.tombstoned_at is not None:
                continue
            if not self.can_grant(op.authority, rid):
                continue
            for pid in op.args["principals"]:
                if pid not in self.s.principal_ids():
                    continue
                if VIEW_RANK[self.s.view_of(rid, pid)] >= VIEW_RANK[view]:
                    continue
                if self.denied(pid, rid, seq, op.authority):
                    continue
                self.s.set_view(rid, pid, view)
                changed.append(rid)
        if not changed:
            raise Rejected("AUTHORITY_MISSING_OR_NO_CHANGE")
        for pid in op.args["principals"]:
            self.s.clear_denials(pid, sorted(set(changed)))
        return OpResult(op, "applied", record_ids=sorted(set(changed)))

    def _op_restrict(self, op: Op, seq: int) -> OpResult:
        view = op.args.get("view", "redacted")
        changed = []
        for rid in op.args["records"]:
            if not self.can_revise(op.authority, rid) and op.authority not in self.s.owners(rid):
                continue
            rec = self.s.record(rid)
            for pid in op.args["principals"]:
                if rec is None or pid == rec.speaker_id:
                    continue
                if VIEW_RANK[self.s.view_of(rid, pid)] > VIEW_RANK[view]:
                    self.s.set_view(rid, pid, view)
                    changed.append(rid)
        if not changed:
            raise Rejected("AUTHORITY_MISSING_OR_NO_CHANGE")
        return OpResult(op, "applied", record_ids=sorted(set(changed)))

    def _op_revoke(self, op: Op, seq: int) -> OpResult:
        changed = []
        records = list(op.args.get("records") or [])
        scope = op.args.get("scope")
        if scope:
            records += [r for r in self.s.records_visible_to(op.authority) if scope in self.s.subjects(r)]
        for rid in dict.fromkeys(records):
            rec = self.s.record(rid)
            if rec is None or rec.tombstoned_at is not None:
                continue
            if not (self.can_revise(op.authority, rid) or op.authority in self.s.owners(rid)):
                continue
            for pid in op.args["principals"]:
                if pid == rec.speaker_id or pid == op.authority:
                    continue
                if self.s.view_of(rid, pid) != "none":
                    self.s.set_view(rid, pid, "none")
                    changed.append(rid)
                self.s.add_denial(pid, rid, seq, op.source_turn)
        if scope:
            for pid in op.args["principals"]:
                self.s.add_denial(pid, scope, seq, op.source_turn)
                self.s.x("DELETE FROM scope_facts WHERE scope_id=? AND principal_id=?", (scope, pid))
        if not changed and not scope:
            raise Rejected("AUTHORITY_MISSING_OR_NO_CHANGE")
        return OpResult(op, "applied", record_ids=sorted(set(changed)))

    def _op_share_scope(self, op: Op, seq: int) -> OpResult:
        scope = op.args["scope"]
        principals = [p for p in op.args["principals"] if p in self.s.principal_ids()]
        if op.authority != scope and op.authority not in self.s.principal_ids():
            raise Rejected("UNKNOWN_PRINCIPAL")
        owned = [r for r in self.s.records_visible_to(op.authority) if scope in self.s.subjects(r)
                 and self.can_grant(op.authority, r)]
        if not owned and op.authority != scope:
            raise Rejected("AUTHORITY_MISSING")
        for pid in principals:
            self.s.add_scope_fact(scope, pid, op.args.get("view", "full"), op.source_turn, op.origin)
        res = self._op_grant(Op("grant", {"records": owned, "principals": principals,
                                          "view": op.args.get("view", "full")}, op.authority, op.source_turn,
                                op.origin), seq) if owned else OpResult(op, "applied")
        return OpResult(op, "applied", record_ids=res.record_ids)

    # ---------------------------------------------------------------- reconcile / revise
    def _op_flag_conflict(self, op: Op, seq: int) -> OpResult:
        a, b = op.args["a"], op.args["b"]
        gid = self.s.new_reorg("flag_conflict", {"a": a, "b": b}, op.evidence, op.confidence, "active", op.source_turn)
        self.s.add_link(a, b, "contradicts", op.confidence, gid)
        return OpResult(op, "applied", record_ids=[a, b])

    def _op_supersede(self, op: Op, seq: int) -> OpResult:
        old, new = op.args["old"], op.args["new"]
        o, n = self.s.record(old), self.s.record(new)
        if o is None or n is None or o.tombstoned_at or n.tombstoned_at:
            raise Rejected("TARGET_TOMBSTONED")
        if o.superseded_by:
            raise Rejected("ALREADY_SUPERSEDED")
        if not self.can_revise(op.authority, old):
            raise Rejected("AUTHORITY_CANNOT_SEE")
        widened = self.s.readers(old) - self.s.readers(new)
        if self.f.keep_readers:
            for pid, view in self.s.views(old).items():
                if VIEW_RANK[view] > VIEW_RANK[self.s.view_of(new, pid)] and not self.denied(pid, new, seq, op.authority):
                    self.s.set_view(new, pid, view)
        self.s.x("UPDATE records SET valid_to=?, superseded_by=?, superseded_seq=? WHERE id=?",
                 (op.args.get("timestamp", ""), new, seq, old))
        if o.slot and not n.slot:
            self.s.x("UPDATE records SET slot=? WHERE id=?", (o.slot, new))
        return OpResult(op, "applied", reason=f"kept_readers={len(widened)}", record_ids=[old, new])

    # ---------------------------------------------------------------- revoke / delete
    def _op_delete(self, op: Op, seq: int) -> OpResult:
        removed: list[str] = []
        restored: list[str] = []
        parts: dict[str, str] = dict(op.args.get("parts") or {})
        removed_texts: list[str] = []
        for rid in op.args["records"]:
            rec = self.s.record(rid)
            if rec is None or rec.tombstoned_at:
                continue
            if self.s.view_of(rid, op.authority) != "full":
                continue
            part = parts.get(rid)
            removed_texts.append(part if (part and self.f.partial_delete) else (self.s.text_owner(rid) or ""))
            if part and self.f.partial_delete:
                text = self.s.text_owner(rid) or ""
                remainder = _remove_part(text, part)
                if remainder and remainder != text:
                    restored.append(self._restore_remainder(rid, remainder, op, seq))
            chain = self.s.chain(rid) if self.f.history_deletion else {rid}
            for cid in chain:
                if cid != rid:
                    removed_texts.append(self.s.text_owner(cid) or "")
                for did in self.s.derivations_of(cid):
                    self.s.tombstone(did, op.source_turn)
                    removed.append(did)
                self.s.tombstone(cid, op.source_turn)
                removed.append(cid)
        if not removed:
            raise Rejected("AUTHORITY_CANNOT_SEE_OR_NOTHING")
        swept = self._sweep_copies(op, seq, set(removed) | set(restored), [t for t in removed_texts if t])
        return OpResult(op, "applied", reason=f"restored={len(restored)} swept={len(swept)}",
                        record_ids=sorted(set(removed)) + restored + swept)

    def _sweep_copies(self, op: Op, seq: int, removed: set[str], removed_texts: list[str]) -> list[str]:
        """Deletion reaches copies (SPEC §6.4): any other record the requester can see that repeats a removed
        specific value (4+ digits: numbers, codes, contact details) or a removed text verbatim is re-stored with
        those values masked; the original is tombstoned. Values are taken from what was removed, never guessed."""
        import re as _re

        from govmem.text import mask, values

        specific = [v for t in removed_texts for v in values(t) if len(_re.sub(r"\D", "", v)) >= 4]
        self.s.forget_values(specific, seq)          # later restatements are masked at write time
        self.s.forget_phrases(removed_texts, seq)
        if not self.f.history_deletion or not removed_texts:
            return []
        whole = [t for t in removed_texts if len(t) >= 20]
        if not specific and not whole:
            return []
        out = []
        for rid in self.s.records_visible_to(op.authority, current_only=False):
            if rid in removed:
                continue
            text = self.s.text_owner(rid) or ""
            hit_vals = [v for v in specific if _re.search(r"(?<![A-Za-z0-9])" + _re.escape(v) + r"(?![A-Za-z0-9])", text)]
            hit_whole = [w for w in whole if is_verbatim(w, text)]
            if not hit_vals and not hit_whole:
                continue
            new_text = text
            for w in hit_whole:
                new_text = _remove_part(new_text, w) or ""
            new_text = mask(new_text, hit_vals) if new_text else ""
            rec = self.s.record(rid)
            if new_text and len(_re.sub(r"\[removed\]", "", new_text).strip()) >= 8 and rec and not rec.superseded_by:
                out.append(self._restore_remainder(rid, new_text, op, seq))
            self.s.tombstone(rid, op.source_turn)
            out.append(rid)
            self.s.bump("delete.swept_copy")
        return out

    def _restore_remainder(self, rid: str, remainder: str, op: Op, seq: int) -> str:
        rec = self.s.record(rid)
        assert rec is not None
        views, subjects = self.s.views(rid), self.s.subjects(rid)
        new = self.s.add_record(text=remainder, speaker_id=rec.speaker_id, source_turn=rec.source_turn,
                                created_seq=rec.created_seq, created_at=rec.created_at, kind=rec.kind, slot=rec.slot)
        self.s.set_subjects(new, subjects)
        for pid, view in views.items():
            self.s.set_view(new, pid, view if view != "redacted" else "existence")
        return new

    def _op_restore_view(self, op: Op, seq: int) -> OpResult:
        changed = []
        for rid in op.args["records"]:
            if not self.can_grant(op.authority, rid):
                continue
            for pid in op.args["principals"]:
                self.s.clear_denials(pid, [rid])
                self.s.set_view(rid, pid, op.args.get("view", "full"))
                changed.append(rid)
        if not changed:
            raise Rejected("AUTHORITY_MISSING")
        return OpResult(op, "applied", record_ids=changed)

    # ---------------------------------------------------------------- organise
    def _organiser_ok(self, op: Op) -> None:
        if op.origin == "organised" and not op.evidence:
            raise Rejected("EVIDENCE_MISSING")

    def _op_annotate(self, op: Op, seq: int) -> OpResult:
        self._organiser_ok(op)
        rid = op.args["record"]
        rec = self.s.record(rid)
        if rec is None or rec.tombstoned_at:
            raise Rejected("TARGET_TOMBSTONED")
        sources = [s for s in op.args.get("sources", []) if s != rid]
        mine = self.s.readers(rid)
        for src in sources:                       # §8.4: a note only uses sources its readers can read
            if not mine <= self.s.readers(src):
                raise Rejected("AUDIENCE_WIDENS")
        gid = self.s.new_reorg("annotate", {"record": rid, "sources": sources}, op.evidence, op.confidence,
                               "active", op.source_turn)
        self.s.add_note(rid, list(op.args.get("keywords") or [])[:12], list(op.args.get("tags") or [])[:8],
                        str(op.args.get("context") or "")[:400], gid)
        text = self.s.text_owner(rid) or ""
        for attr in op.args.get("attributes") or []:
            span = str(attr.get("span") or attr.get("value") or "")
            if span and is_verbatim(span, text):
                self.s.add_attribute(rid, str(attr.get("name") or "value")[:60], str(attr.get("type") or "text"),
                                     str(attr.get("value") or span)[:200], gid)
            else:
                self.s.bump("annotate.attribute_not_verbatim")
        return OpResult(op, "applied", record_ids=[rid])

    def _op_link(self, op: Op, seq: int) -> OpResult:
        self._organiser_ok(op)
        a, b, kind = op.args["a"], op.args["b"], op.args["kind"]
        if kind not in LINK_KINDS:
            raise Rejected("BAD_LINK_KIND")
        ra, rb = self.s.record(a), self.s.record(b)
        if ra is None or rb is None or ra.tombstoned_at or rb.tombstoned_at or a == b:
            raise Rejected("TARGET_TOMBSTONED")
        status = "pending" if kind == "same_value_slot" else "active"
        gid = self.s.new_reorg("link", {"a": a, "b": b, "kind": kind}, op.evidence, op.confidence, status,
                               op.source_turn)
        self.s.add_link(a, b, kind, op.confidence, gid, status=status)
        return OpResult(op, status, record_ids=[a, b])

    def _op_alias(self, op: Op, seq: int) -> OpResult:
        name, target = str(op.args["alias"]).strip(), op.args["target"]
        if target not in self.s.principal_ids() | self.s.entity_ids():
            raise Rejected("UNKNOWN_TARGET")
        if not op.evidence.get("turn_text") or not is_verbatim(name, op.evidence["turn_text"]):
            raise Rejected("EVIDENCE_MISSING")
        if len(name) < 3:
            raise Rejected("ALIAS_TOO_SHORT")
        gid = self.s.new_reorg("alias", {"alias": name, "target": target}, {"turn": op.source_turn},
                               op.confidence, "active", op.source_turn)
        self.s.add_alias(name.lower(), target, op.origin, gid)
        return OpResult(op, "applied")

    def _op_retract(self, op: Op, seq: int) -> OpResult:
        gid = op.args["reorg"]
        r = self.s.q("SELECT * FROM _reorgs WHERE id=?", (gid,))
        if not r or r[0]["status"] == "retracted":
            raise Rejected("UNKNOWN_REORG")
        kind = r[0]["op"]
        if kind == "annotate":
            rows = self.s.q("SELECT record_id, version FROM notes WHERE reorg_id=?", (gid,))
            for row in rows:
                self.s.x("UPDATE notes SET status='retracted' WHERE record_id=? AND version=?",
                         (row["record_id"], row["version"]))
                prev = self.s.q("SELECT MAX(version) FROM notes WHERE record_id=? AND status='previous'",
                                (row["record_id"],))
                if prev and prev[0][0]:
                    self.s.x("UPDATE notes SET status='active' WHERE record_id=? AND version=?",
                             (row["record_id"], prev[0][0]))
            self.s.x("UPDATE attributes SET status='retracted' WHERE reorg_id=?", (gid,))
        elif kind in ("link", "flag_conflict"):
            self.s.x("UPDATE links SET status='retracted' WHERE reorg_id=?", (gid,))
        elif kind == "alias":
            self.s.x("UPDATE aliases SET status='retracted' WHERE reorg_id=?", (gid,))
        self.s.set_reorg_status(gid, "retracted", op.source_turn, str(op.args.get("reason") or ""))
        return OpResult(op, "applied")


def _remove_part(text: str, part: str) -> str:
    import re

    pat = re.escape(part.strip()).replace(r"\ ", r"\s+")
    out = re.sub(pat, " ", text, count=1, flags=re.IGNORECASE)
    out = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s+", " ", out)).strip(" ,;")
    return out if len(out) >= 8 else ""


def _loggable(args: dict[str, Any]) -> dict[str, Any]:
    """Content-free view of args for the op log: ids, counts and lengths, never text."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in ("text", "redacted_text", "verbatim_of", "context", "keywords", "tags", "attributes",
                 "protected_values", "parts", "alias", "timestamp"):
            out[k] = f"<{len(v) if hasattr(v, '__len__') else 1}>"
        elif isinstance(v, (list, tuple, set)):
            out[k] = sorted(map(str, v))[:20]
        elif isinstance(v, dict):
            out[k] = sorted(map(str, v.keys()))[:20]
        else:
            out[k] = v
    return out
