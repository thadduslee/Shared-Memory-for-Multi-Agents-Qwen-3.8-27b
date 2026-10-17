"""Who is declared, how turns refer to them, and the policy facts.

Nothing here knows a relationship type, a role or a key name from any
dataset. A declared relationship is read structurally:

  * its LINKS are the values that are declared principal ids, plus the values
    of keys spelled as identifiers (`id` or `*_id`) -- the things it connects;
  * its HOLDERS are the links that are principals;
  * every other key is an ATTRIBUTE, kept verbatim for the prompts to read;
  * it is QUALIFIED when it carries any attribute besides `type`. The
    scaffold cannot interpret a qualifier (that is the prompts' job), so the
    deterministic fallback treats a qualified relationship as granting
    nothing -- the safe direction.

Each declared relationship becomes one row of `_policy` (a policy *fact*).
Consents and revocations read from turns add or end facts, so the facts are
the episode's current answer to "who is connected to what", and the prompts
are shown them, not the reset-time declarations.

Mentions: a turn rarely spells a declared id. A principal is also referred to
by any token of its id or display name that no other declared id shares (so
a first name works, a shared surname does not); an entity id by any of its
own tokens no other id shares. All of that is computed from the episode's own
declarations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from memory_system.terms import STOPWORDS, words


@dataclass
class Principal:
    principal_id: str
    role: str
    display_name: str = ""


def _id_like_key(key: str) -> bool:
    return key == "id" or key.endswith("_id")


def parse_relationship(rel: dict[str, Any], principal_ids: set[str]) -> dict[str, Any]:
    links: list[str] = []
    attributes: dict[str, Any] = {}
    for key, value in rel.items():
        values = value if isinstance(value, list) else [value]
        linked = [
            v for v in values if isinstance(v, str) and v and (v in principal_ids or _id_like_key(str(key)))
        ]
        if linked and len(linked) == len(values):
            links += [v for v in linked if v not in links]
        else:
            attributes[str(key)] = value
    holders = [x for x in links if x in principal_ids]
    qualified = any(k != "type" for k in attributes)
    return {"links": links, "holders": holders, "attributes": attributes, "qualified": qualified}


def _alias_tokens(text: str) -> set[str]:
    return {
        w for w in words(text.replace("_", " ")) if len(w) >= 3 and not w.isdigit() and w not in STOPWORDS
    }


class Directory:
    """The episode's declared principals and entity ids, and how turns name them."""

    def __init__(self, principals: list[Principal], relationships: list[dict[str, Any]]) -> None:
        self.principals: dict[str, Principal] = {p.principal_id: p for p in principals}
        pids = set(self.principals)
        self.relationships = [parse_relationship(r, pids) for r in relationships if isinstance(r, dict)]
        self.entities: list[str] = []
        for rel in self.relationships:
            self.entities += [x for x in rel["links"] if x not in pids and x not in self.entities]
        owners: dict[str, set[str]] = {}
        for pid, p in self.principals.items():
            for tok in _alias_tokens(pid) | _alias_tokens(p.display_name or ""):
                owners.setdefault(tok, set()).add(pid)
        for eid in self.entities:
            for tok in _alias_tokens(eid):
                owners.setdefault(tok, set()).add(eid)
        self.aliases: dict[str, str] = {t: next(iter(o)) for t, o in owners.items() if len(o) == 1}
        self._full = {i.lower(): i for i in [*self.principals, *self.entities]}
        self._odd = [i for i in self._full if not re.fullmatch(r"[a-z0-9_]+", i)]

    @classmethod
    def from_entities(cls, entities: dict[str, Any] | None) -> Directory:
        entities = entities or {}
        principals = [
            Principal(str(p.get("principal_id")), str(p.get("role") or ""), str(p.get("display_name") or ""))
            for p in entities.get("principals") or []
            if isinstance(p, dict) and p.get("principal_id")
        ]
        return cls(principals, list(entities.get("relationships") or []))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Directory:
        """Rebuild from the `declarations` block the prompts are shown."""
        principals = [
            Principal(p["id"], p.get("role", ""), p.get("name", "")) for p in payload.get("principals") or []
        ]
        # An entity id rides in as the value of an id-shaped key, which is
        # exactly how parse_relationship recognises a link.
        return cls(principals, [{"_id": e} for e in payload.get("entities") or []])

    def is_principal(self, x: str) -> bool:
        return x in self.principals

    def is_declared(self, x: str) -> bool:
        return x in self.principals or x in self.entities

    def role(self, pid: str) -> str:
        p = self.principals.get(pid)
        return p.role if p else ""

    def mentions(self, text: str) -> list[str]:
        """Declared ids a text refers to, in order of first mention."""
        found: list[str] = []
        lowered = (text or "").lower()
        for w in words(text):
            hit = self._full.get(w) or self.aliases.get(w)
            if hit and hit not in found:
                found.append(hit)
        for odd in self._odd:
            if odd in lowered and self._full[odd] not in found:
                found.append(self._full[odd])
        return found

    def id_terms(self) -> frozenset[str]:
        """Word terms that only name a declared id. Matching on these says
        which thing a text is about, not which fact about it."""
        return frozenset(self.aliases) | frozenset(self._full)

    def payload(self) -> dict[str, Any]:
        return {
            "principals": [
                {"id": p.principal_id, "role": p.role, "name": p.display_name}
                for p in self.principals.values()
            ],
            "entities": list(self.entities),
        }


# ---------------------------------------------------------------- policy facts


def add_fact(
    conn: sqlite3.Connection,
    *,
    links: list[str],
    holders: list[str],
    attributes: dict[str, Any],
    qualified: bool,
    source: str,
    at: str,
) -> str:
    n = conn.execute("SELECT COUNT(*) FROM _policy").fetchone()[0]
    fid = f"f{n + 1:03d}"
    conn.execute(
        "INSERT INTO _policy(fact_id, links, holders, qualified, attributes, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            fid,
            json.dumps(links),
            json.dumps(holders),
            int(qualified),
            json.dumps(attributes, default=str),
            source,
            at,
        ),
    )
    return fid


def seed_policy(conn: sqlite3.Connection, directory: Directory) -> None:
    for rel in directory.relationships:
        add_fact(
            conn,
            links=rel["links"],
            holders=rel["holders"],
            attributes=rel["attributes"],
            qualified=rel["qualified"],
            source="declared",
            at="",
        )


def current_facts(conn: sqlite3.Connection, involving: list[str] | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT fact_id, links, holders, qualified, attributes, source, created_at FROM _policy "
        "WHERE valid_to IS NULL ORDER BY fact_id"
    ).fetchall()
    out = []
    want = set(involving or [])
    for fid, links, holders, qualified, attrs, source, created in rows:
        links_l = json.loads(links)
        if want and not want & set(links_l):
            continue
        out.append(
            {
                "id": fid,
                "links": links_l,
                "holders": json.loads(holders),
                "qualified": bool(qualified),
                "attributes": json.loads(attrs),
                "source": source,
                "since": created,
            }
        )
    return out


def compact(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Facts as the prompts see them: links and attributes, `qualified` only
    when true, `since` only for facts read from turns. Holders are the links
    that are principals, which the prompt input lists separately."""
    out = []
    for f in facts:
        row: dict[str, Any] = {"links": f["links"]}
        if f["attributes"]:
            row["attributes"] = f["attributes"]
        if f["qualified"]:
            row["qualified"] = True
        if f["source"] != "declared":
            row["since"] = f["source"]
        out.append(row)
    return out


def group(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Holders of current UNQUALIFIED facts that link any of `ids`. The
    fallback's reading of 'who shares in what this is about'."""
    members: set[str] = set()
    for fact in current_facts(conn, involving=ids):
        if not fact["qualified"]:
            members |= set(fact["holders"])
    return members


def end_facts(conn: sqlite3.Connection, principal_id: str, scope: list[str], at: str) -> list[str]:
    """End every current fact in which `principal_id` holds a link to any of
    `scope`. Returns the ended fact ids."""
    ended: list[str] = []
    for fact in current_facts(conn, involving=scope):
        if principal_id in fact["holders"] and set(fact["links"]) & (set(scope) - {principal_id}):
            conn.execute("UPDATE _policy SET valid_to = ? WHERE fact_id = ?", (at, fact["id"]))
            ended.append(fact["id"])
    return ended
