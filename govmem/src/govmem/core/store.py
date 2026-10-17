"""Store: typed access to the fixed tables. Trusted code only; models never call this directly.

Views and key wraps are kept in step here (SPEC §6.1 `record_views` mirrors `_keys`):
  full      -> wraps for the record key and the redacted key
  redacted  -> wrap for the redacted key only
  existence -> no wraps; the row only records that the principal may know it exists
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import numpy as np

from govmem.core import keyring
from govmem.core.keyring import OWNER
from govmem.core.schema import DDL
from govmem.embed import from_blob, to_blob
from govmem.text import tokens, value_key

VIEW_RANK = {"none": 0, "existence": 1, "redacted": 2, "full": 3}


def red_key(record_id: str) -> str:
    return record_id + "#r"


@dataclass
class RecordRow:
    id: str
    speaker_id: str
    source_turn: str
    created_seq: int
    slot: str | None
    kind: str
    superseded_by: str | None
    superseded_seq: int | None
    tombstoned_at: str | None
    has_redacted: bool
    derived_from: list[str]
    created_at: str

    @property
    def current(self) -> bool:
        return self.tombstoned_at is None and self.superseded_by is None


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        keyring.ensure_schema(self.conn)
        row = self.conn.execute("SELECT v FROM meta WHERE k='secret'").fetchone()
        if row is None:
            self.secret = os.urandom(32)
            self.conn.execute("INSERT INTO meta(k, v) VALUES ('secret', ?)", (self.secret.hex(),))
            self.conn.execute("INSERT INTO meta(k, v) VALUES ('next_record', '1')")
            self.conn.execute("INSERT INTO meta(k, v) VALUES ('next_reorg', '1')")
        else:
            self.secret = bytes.fromhex(row[0])
        self._tx_depth = 0

    # ------------------------------------------------------------ transactions
    @contextmanager
    def atomic(self) -> Iterator[None]:
        name = f"sp{self._tx_depth}"
        self._tx_depth += 1
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")
        finally:
            self._tx_depth -= 1

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    def _next(self, counter: str, prefix: str) -> str:
        n = int(self.q("SELECT v FROM meta WHERE k=?", (counter,))[0][0])
        self.x("UPDATE meta SET v=? WHERE k=?", (str(n + 1), counter))
        return f"{prefix}{n:05d}"

    def fingerprint(self, s: str) -> str:
        return hmac.new(self.secret, s.encode(), hashlib.sha256).hexdigest()[:32]

    # ------------------------------------------------------------ declarations
    def declare_principal(self, pid: str, role: str, display_name: str = "", kind: str = "unknown",
                          acts_for: str | None = None) -> None:
        self.x("INSERT OR REPLACE INTO principals(id, role, display_name, kind, acts_for) VALUES (?,?,?,?,?)",
               (pid, role, display_name, kind, acts_for))

    def declare_entity(self, eid: str, kind_label: str = "") -> None:
        self.x("INSERT OR IGNORE INTO entities(id, kind_label) VALUES (?, ?)", (eid, kind_label))

    def declare_relationship(self, rtype: str, endpoints: dict[str, str], principals: list[str],
                             entities: list[str], attributes: dict[str, Any]) -> int:
        cur = self.x("INSERT INTO relationships(type, endpoints, principals, entities, attributes, qualified) "
                     "VALUES (?,?,?,?,?,?)",
                     (rtype, json.dumps(endpoints), json.dumps(principals), json.dumps(entities),
                      json.dumps(attributes), int(bool(attributes))))
        return int(cur.lastrowid)

    def principals(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM principals ORDER BY rowid")

    def principal_ids(self) -> set[str]:
        return {r["id"] for r in self.principals()}

    def entity_ids(self) -> set[str]:
        return {r["id"] for r in self.q("SELECT id FROM entities")}

    def relationships(self) -> list[dict[str, Any]]:
        out = []
        for r in self.q("SELECT * FROM relationships ORDER BY id"):
            out.append({"id": r["id"], "type": r["type"], "endpoints": json.loads(r["endpoints"]),
                        "principals": json.loads(r["principals"]), "entities": json.loads(r["entities"]),
                        "attributes": json.loads(r["attributes"]), "qualified": bool(r["qualified"])})
        return out

    def role_of(self, pid: str) -> str:
        r = self.q("SELECT role FROM principals WHERE id=?", (pid,))
        return r[0]["role"] if r else ""

    # ------------------------------------------------------------ turns
    def register_turn(self, turn_id: str, ts: str, speaker_id: str) -> int:
        r = self.q("SELECT seq FROM turns WHERE turn_id=?", (turn_id,))
        if r:
            return int(r[0]["seq"])
        cur = self.x("INSERT INTO turns(turn_id, ts, speaker_id) VALUES (?,?,?)", (turn_id, ts, speaker_id))
        return int(cur.lastrowid)

    def turn_seq(self, turn_id: str) -> int | None:
        r = self.q("SELECT seq FROM turns WHERE turn_id=?", (turn_id,))
        return int(r[0]["seq"]) if r else None

    def last_seq(self) -> int:
        r = self.q("SELECT MAX(seq) FROM turns")
        return int(r[0][0] or 0)

    # ------------------------------------------------------------ records
    def add_record(self, *, text: str, speaker_id: str, source_turn: str, created_seq: int, created_at: str,
                   kind: str, slot: str | None, redacted_text: str | None = None,
                   derived_from: list[str] | None = None) -> str:
        rid = self._next("next_record", "r")
        keyring.new_key(self.conn, rid)
        body = keyring.seal(self.conn, rid, "body", text)
        has_red = 0
        if redacted_text:
            keyring.new_key(self.conn, red_key(rid))
            self.x("INSERT INTO record_redacted(record_id, body) VALUES (?, ?)",
                   (rid, keyring.seal(self.conn, red_key(rid), "body", redacted_text)))
            has_red = 1
        self.x("INSERT INTO records(id, body, speaker_id, source_turn, created_seq, created_at, valid_from, slot, "
               "derived_from, kind, has_redacted) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (rid, body, speaker_id, source_turn, created_seq, created_at, created_at, slot,
                json.dumps(derived_from or []), kind, has_red))
        self.index_terms(rid, text)
        return rid

    def record(self, rid: str) -> RecordRow | None:
        r = self.q("SELECT * FROM records WHERE id=?", (rid,))
        if not r:
            return None
        r = r[0]
        return RecordRow(id=r["id"], speaker_id=r["speaker_id"], source_turn=r["source_turn"],
                         created_seq=r["created_seq"], slot=r["slot"], kind=r["kind"] or "",
                         superseded_by=r["superseded_by"], superseded_seq=r["superseded_seq"],
                         tombstoned_at=r["tombstoned_at"], has_redacted=bool(r["has_redacted"]),
                         derived_from=json.loads(r["derived_from"] or "[]"), created_at=r["created_at"] or "")

    def records(self, *, current_only: bool = True, alive_only: bool = True) -> list[RecordRow]:
        sql = "SELECT id FROM records WHERE 1=1"
        if alive_only:
            sql += " AND tombstoned_at IS NULL"
        if current_only:
            sql += " AND superseded_by IS NULL"
        return [self.record(r["id"]) for r in self.q(sql + " ORDER BY created_seq, id")]  # type: ignore[misc]

    def text_owner(self, rid: str) -> str | None:
        """Plaintext for trusted maintenance (interpreter targeting, organiser, indexing). Never for release."""
        r = self.q("SELECT body FROM records WHERE id=?", (rid,))
        if not r:
            return None
        return keyring.open_for(self.conn, rid, OWNER, "body", r[0]["body"])

    def open_as(self, rid: str, principal_id: str) -> tuple[str | None, str]:
        """The release path: (text, view) the principal may receive. Goes through the keyring with the
        principal's own identity; nothing else produces released text (I-1)."""
        view = self.view_of(rid, principal_id)
        if view == "full":
            r = self.q("SELECT body FROM records WHERE id=? AND tombstoned_at IS NULL", (rid,))
            if r:
                return keyring.open_for(self.conn, rid, principal_id, "body", r[0]["body"]), "full"
        if view in ("full", "redacted"):
            r = self.q("SELECT body FROM record_redacted WHERE record_id=?", (rid,))
            if r:
                return keyring.open_for(self.conn, red_key(rid), principal_id, "body", r[0]["body"]), "redacted"
        return None, view

    # ------------------------------------------------------------ subjects
    def set_subjects(self, rid: str, subjects: dict[str, bool]) -> None:
        for sid, owner in subjects.items():
            self.x("INSERT OR REPLACE INTO record_subjects(record_id, subject_id, is_owner) VALUES (?,?,?)",
                   (rid, sid, int(owner)))

    def subjects(self, rid: str) -> dict[str, bool]:
        return {r["subject_id"]: bool(r["is_owner"]) for r in
                self.q("SELECT subject_id, is_owner FROM record_subjects WHERE record_id=?", (rid,))}

    def owners(self, rid: str) -> set[str]:
        return {s for s, o in self.subjects(rid).items() if o}

    # ------------------------------------------------------------ views (mirrors keys)
    def view_of(self, rid: str, pid: str) -> str:
        r = self.q("SELECT view FROM record_views WHERE record_id=? AND principal_id=?", (rid, pid))
        return r[0]["view"] if r else "none"

    def views(self, rid: str) -> dict[str, str]:
        return {r["principal_id"]: r["view"] for r in
                self.q("SELECT principal_id, view FROM record_views WHERE record_id=?", (rid,))}

    def full_audience(self, rid: str) -> set[str]:
        return {p for p, v in self.views(rid).items() if v == "full"}

    def readers(self, rid: str) -> set[str]:
        return {p for p, v in self.views(rid).items() if v in ("full", "redacted")}

    def set_view(self, rid: str, pid: str, view: str) -> None:
        rec = self.record(rid)
        if rec is None or rec.tombstoned_at is not None:
            return
        if view == "redacted" and not rec.has_redacted:
            view = "existence"
        if view == "none":
            self.x("DELETE FROM record_views WHERE record_id=? AND principal_id=?", (rid, pid))
        else:
            self.x("INSERT OR REPLACE INTO record_views(record_id, principal_id, view) VALUES (?,?,?)",
                   (rid, pid, view))
        if view == "full":
            keyring.grant(self.conn, rid, pid)
            if rec.has_redacted:
                keyring.grant(self.conn, red_key(rid), pid)
        elif view == "redacted":
            keyring.revoke(self.conn, rid, pid)
            keyring.grant(self.conn, red_key(rid), pid)
        else:
            keyring.revoke(self.conn, rid, pid)
            keyring.revoke(self.conn, red_key(rid), pid)

    def records_visible_to(self, pid: str, *, min_view: str = "full", current_only: bool = True) -> list[str]:
        allowed = [v for v, k in VIEW_RANK.items() if k >= VIEW_RANK[min_view]]
        marks = ",".join("?" * len(allowed))
        sql = (f"SELECT r.id FROM records r JOIN record_views v ON v.record_id = r.id "
               f"WHERE v.principal_id=? AND v.view IN ({marks}) AND r.tombstoned_at IS NULL")
        if current_only:
            sql += " AND r.superseded_by IS NULL"
        return [r["id"] for r in self.q(sql + " ORDER BY r.created_seq", (pid, *allowed))]

    # ------------------------------------------------------------ denials, scope facts
    def add_denial(self, pid: str, target: str, seq: int, turn: str) -> None:
        self.x("INSERT INTO _denials(principal_id, target, created_seq, source_turn) VALUES (?,?,?,?)",
               (pid, target, seq, turn))

    def latest_denial(self, pid: str, targets: list[str]) -> int | None:
        if not targets:
            return None
        marks = ",".join("?" * len(targets))
        r = self.q(f"SELECT MAX(created_seq) FROM _denials WHERE principal_id=? AND target IN ({marks})",
                   (pid, *targets))
        return int(r[0][0]) if r and r[0][0] is not None else None

    def clear_denials(self, pid: str, targets: list[str]) -> None:
        for t in targets:
            self.x("DELETE FROM _denials WHERE principal_id=? AND target=?", (pid, t))

    def add_scope_fact(self, scope_id: str, pid: str, view: str, turn: str, origin: str) -> None:
        self.x("INSERT INTO scope_facts(scope_id, principal_id, view, source_turn, origin) VALUES (?,?,?,?,?)",
               (scope_id, pid, view, turn, origin))

    def scope_facts_for(self, scope_ids: list[str]) -> dict[str, str]:
        if not scope_ids:
            return {}
        marks = ",".join("?" * len(scope_ids))
        out: dict[str, str] = {}
        for r in self.q(f"SELECT principal_id, view FROM scope_facts WHERE scope_id IN ({marks})", tuple(scope_ids)):
            if VIEW_RANK[r["view"]] > VIEW_RANK.get(out.get(r["principal_id"], "none"), 0):
                out[r["principal_id"]] = r["view"]
        return out

    # ------------------------------------------------------------ protected values
    def add_protected(self, rid: str, values: list[str]) -> None:
        for v in values:
            k = value_key(v)
            if k:
                self.x("INSERT INTO protected_values(record_id, fingerprint) VALUES (?, ?)", (rid, self.fingerprint(k)))

    def protected_fingerprints(self, exclude: set[str]) -> dict[str, str]:
        out = {}
        for r in self.q("SELECT p.record_id, p.fingerprint FROM protected_values p JOIN records r ON r.id = p.record_id "
                        "WHERE r.tombstoned_at IS NULL"):
            if r["record_id"] not in exclude:
                out[r["fingerprint"]] = r["record_id"]
        return out

    # ------------------------------------------------------------ forgotten values (keyed hashes only)
    def forget_values(self, vals: list[str], seq: int) -> None:
        for v in vals:
            k = value_key(v)
            if k:
                self.x("INSERT OR IGNORE INTO _forgotten(fingerprint, created_seq) VALUES (?, ?)",
                       (self.fingerprint("forgotten:" + k), seq))

    def forget_phrases(self, texts: list[str], seq: int) -> None:
        """Names (2+ capitalised words) and 5-word runs of deleted texts, as keyed hashes."""
        import re as _re

        from govmem.text import clean

        for t in texts:
            for m in _re.finditer(r"\b[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,4}\b", clean(t)):
                self.forget_values([m.group(0)], seq)
            toks = value_key(t).split()
            for i in range(0, max(0, len(toks) - 4)):
                self.forget_values([" ".join(toks[i:i + 5])], seq)

    def forgotten_phrases_in(self, text: str) -> list[str]:
        """Exact substrings of `text` (2-8 words) whose keyed hash was forgotten."""
        import re as _re

        from govmem.text import clean

        words = list(_re.finditer(r"[A-Za-z0-9][A-Za-z0-9'.\-]*", clean(text)))
        hits: list[tuple[int, int]] = []
        for n in range(8, 1, -1):
            for i in range(0, len(words) - n + 1):
                a, b = words[i].start(), words[i + n - 1].end()
                if any(a < y and b > x for x, y in hits):
                    continue
                if self.forgotten_in([clean(text)[a:b]]):
                    hits.append((a, b))
        return [clean(text)[a:b] for a, b in sorted(hits)]

    def forgotten_in(self, vals: list[str]) -> list[str]:
        return [v for v in vals if value_key(v) and self.q("SELECT 1 FROM _forgotten WHERE fingerprint=?",
                                                          (self.fingerprint("forgotten:" + value_key(v)),))]

    # ------------------------------------------------------------ terms and embeddings
    def index_terms(self, rid: str, text: str) -> None:
        for t in set(tokens(text)):
            self.x("INSERT OR IGNORE INTO terms(record_id, term_hash) VALUES (?, ?)", (rid, self.fingerprint("t:" + t)))

    def term_hashes(self, words: list[str]) -> list[str]:
        return [self.fingerprint("t:" + w) for w in words]

    def set_embedding(self, rid: str, vec: np.ndarray, note_version: int = 0) -> None:
        self.x("INSERT OR REPLACE INTO embeddings(record_id, note_version, vector) VALUES (?,?,?)",
               (rid, note_version, to_blob(vec)))

    def embeddings(self, rids: list[str]) -> dict[str, np.ndarray]:
        out = {}
        for i in range(0, len(rids), 500):
            chunk = rids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.q(f"SELECT record_id, vector FROM embeddings WHERE record_id IN ({marks})", tuple(chunk)):
                out[r["record_id"]] = from_blob(r["vector"])
        return out

    # ------------------------------------------------------------ notes, attributes, links, clusters, aliases
    def active_note(self, rid: str) -> dict[str, Any] | None:
        r = self.q("SELECT * FROM notes WHERE record_id=? AND status='active' ORDER BY version DESC LIMIT 1", (rid,))
        if not r:
            return None
        r = r[0]
        v = r["version"]

        def op(col: str) -> str:
            return keyring.open_for(self.conn, rid, OWNER, f"note:{v}:{col}", r[col]) or ""

        return {"version": v, "keywords": json.loads(op("keywords") or "[]"), "tags": json.loads(op("tags") or "[]"),
                "context": op("context"), "reorg_id": r["reorg_id"]}

    def add_note(self, rid: str, keywords: list[str], tags: list[str], context: str, reorg_id: str) -> int:
        r = self.q("SELECT MAX(version) FROM notes WHERE record_id=?", (rid,))
        v = int(r[0][0] or 0) + 1
        self.x("UPDATE notes SET status='previous' WHERE record_id=? AND status='active'", (rid,))
        self.x("INSERT INTO notes(record_id, version, keywords, tags, context, status, reorg_id) VALUES (?,?,?,?,?,?,?)",
               (rid, v, keyring.seal(self.conn, rid, f"note:{v}:keywords", json.dumps(keywords)),
                keyring.seal(self.conn, rid, f"note:{v}:tags", json.dumps(tags)),
                keyring.seal(self.conn, rid, f"note:{v}:context", context), "active", reorg_id))
        self.index_terms(rid, " ".join(keywords + tags))
        return v

    def add_attribute(self, rid: str, name: str, typ: str, value: str, reorg_id: str) -> int:
        cur = self.x("INSERT INTO attributes(record_id, name, type, value, status, reorg_id) VALUES (?,?,?,?,?,?)",
                     (rid, name, typ, b"", "active", reorg_id))
        aid = int(cur.lastrowid)
        self.x("UPDATE attributes SET value=? WHERE id=?", (keyring.seal(self.conn, rid, f"attr:{aid}", value), aid))
        return aid

    def attributes(self, rid: str) -> list[dict[str, Any]]:
        out = []
        for r in self.q("SELECT * FROM attributes WHERE record_id=? AND status='active'", (rid,)):
            out.append({"id": r["id"], "name": r["name"], "type": r["type"],
                        "value": keyring.open_for(self.conn, rid, OWNER, f"attr:{r['id']}", r["value"]) or ""})
        return out

    def add_link(self, a: str, b: str, kind: str, strength: float, reorg_id: str, status: str = "active") -> int:
        cur = self.x("INSERT INTO links(a, b, kind, strength, status, reorg_id) VALUES (?,?,?,?,?,?)",
                     (a, b, kind, strength, status, reorg_id))
        return int(cur.lastrowid)

    def links_of(self, rid: str, status: str = "active") -> list[sqlite3.Row]:
        return self.q("SELECT * FROM links WHERE (a=? OR b=?) AND status=?", (rid, rid, status))

    def new_reorg(self, op: str, args: dict[str, Any], evidence: dict[str, Any], confidence: float, status: str,
                  turn: str, reason: str = "") -> str:
        gid = self._next("next_reorg", "g")
        self.x("INSERT INTO _reorgs(id, op, args, evidence, confidence, status, created_turn, decided_turn, reason) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (gid, op, json.dumps(args, sort_keys=True), json.dumps(evidence, sort_keys=True), confidence, status,
                turn, turn if status != "pending" else None, reason))
        return gid

    def set_reorg_status(self, gid: str, status: str, turn: str, reason: str = "") -> None:
        self.x("UPDATE _reorgs SET status=?, decided_turn=?, reason=? WHERE id=?", (status, turn, reason, gid))

    def aliases(self) -> list[tuple[str, str, str]]:
        return [(r["alias"], r["target_id"], r["source"]) for r in
                self.q("SELECT alias, target_id, source FROM aliases WHERE status='active'")]

    def add_alias(self, alias: str, target: str, source: str, reorg_id: str | None = None) -> None:
        self.x("INSERT OR IGNORE INTO aliases(alias, target_id, source, status, reorg_id) VALUES (?,?,?,?,?)",
               (alias, target, source, "active", reorg_id))

    # ------------------------------------------------------------ deletion
    def tombstone(self, rid: str, when: str) -> None:
        """Delete + shred: body, redacted body, notes, attributes, links, clusters, embeddings, terms, keys."""
        self.x("UPDATE records SET tombstoned_at=?, body=NULL WHERE id=?", (when, rid))
        self.x("DELETE FROM record_redacted WHERE record_id=?", (rid,))
        self.x("DELETE FROM record_views WHERE record_id=?", (rid,))
        self.x("DELETE FROM protected_values WHERE record_id=?", (rid,))
        self.x("DELETE FROM notes WHERE record_id=?", (rid,))
        self.x("DELETE FROM attributes WHERE record_id=?", (rid,))
        self.x("DELETE FROM links WHERE a=? OR b=?", (rid, rid))
        self.x("DELETE FROM cluster_members WHERE record_id=?", (rid,))
        self.x("DELETE FROM embeddings WHERE record_id=?", (rid,))
        self.x("DELETE FROM terms WHERE record_id=?", (rid,))
        keyring.shred(self.conn, rid)
        keyring.shred(self.conn, red_key(rid))

    def chain(self, rid: str) -> set[str]:
        """Every version linked to `rid` by supersession, both directions."""
        seen: set[str] = set()
        todo = [rid]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for r in self.q("SELECT id FROM records WHERE superseded_by=?", (cur,)):
                todo.append(r["id"])
            r = self.q("SELECT superseded_by FROM records WHERE id=?", (cur,))
            if r and r[0]["superseded_by"]:
                todo.append(r[0]["superseded_by"])
        return seen

    def derivations_of(self, rid: str) -> list[str]:
        return [r["id"] for r in self.q("SELECT id, derived_from FROM records WHERE tombstoned_at IS NULL")
                if rid in json.loads(r["derived_from"] or "[]")]

    # ------------------------------------------------------------ log, signals, health
    def log(self, turn: str, op: str, args: dict[str, Any], authority: str, origin: str, outcome: str,
            reason: str = "") -> None:
        self.x("INSERT INTO _op_log(turn, op, args, authority, origin, outcome, reason_code) VALUES (?,?,?,?,?,?,?)",
               (turn, op, json.dumps(args, sort_keys=True, default=str), authority, origin, outcome, reason))

    def signal(self, turn: str, kind: str, refs: dict[str, Any]) -> None:
        self.x("INSERT INTO _signals(turn, kind, refs) VALUES (?,?,?)", (turn, kind, json.dumps(refs, sort_keys=True)))

    def bump(self, counter: str, n: int = 1) -> None:
        self.x("INSERT INTO _health(counter, value) VALUES (?, ?) ON CONFLICT(counter) DO UPDATE SET value = value + ?",
               (counter, n, n))

    def health(self) -> dict[str, int]:
        return {r["counter"]: r["value"] for r in self.q("SELECT counter, value FROM _health")}
