"""The roster: who is in the conversation, the closed audience menu, and reference resolution (SPEC §7.2-7.3).

Everything here is derived from the conversation's own declarations; nothing about roles or relationship
types is known in advance.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from govmem.core.store import Store

SPECIAL = ("speaker", "about", "everyone")


@dataclass
class Mention:
    alias: str
    target: str
    start: int
    end: int


class Roster:
    def __init__(self, store: Store) -> None:
        self.s = store
        self.active: set[str] | None = None      # None: the whole declared cast (no window)
        self.refresh()

    def set_active(self, ids: set[str] | None) -> None:
        """Restrict the menu and group tokens (everyone, role:, group:, entities) to recently active people."""
        self.active = set(ids) if ids is not None else None

    def _pool(self) -> set[str]:
        return set(self.principals) if self.active is None else (set(self.principals) & self.active)

    def refresh(self) -> None:
        self.principals = {r["id"]: (r["role"] or "", r["display_name"] or "") for r in self.s.principals()}
        self.entities = sorted(self.s.entity_ids())
        self.relationships = self.s.relationships()
        self.roles = sorted({role for role, _ in self.principals.values() if role})
        self._build_aliases()

    # ------------------------------------------------------------ menu
    def render(self) -> str:
        pool = self._pool()
        lines = ["People (id | role | name):"]
        for pid, (role, name) in self.principals.items():
            if pid in pool:
                lines.append(f"- {pid} | {role} | {name}")
        if self.entities:
            lines.append("Declared things (id): " + ", ".join(self.entities))
        if self.relationships:
            lines.append("Declared relationship groups:")
            for rel in self.relationships:
                if self.active is not None and not (set(rel["principals"]) & pool):
                    continue
                attrs = "; ".join(f"{k}={v}" for k, v in rel["attributes"].items())
                ends = ", ".join(f"{k}={v}" for k, v in rel["endpoints"].items())
                lines.append(f"- group:{rel['id']} | {rel['type']} | {ends}" + (f" | {attrs}" if attrs else ""))
        roles = sorted({self.principals[p][0] for p in pool if self.principals[p][0]})
        lines.append("Role tokens: " + ", ".join(f"role:{r}" for r in roles))
        return "\n".join(lines)

    def valid_token(self, tok: str) -> bool:
        tok = str(tok)
        if tok in SPECIAL or tok in self.principals or tok in self.entities:
            return True
        if tok.startswith("group:"):
            return any(str(r["id"]) == tok[6:] for r in self.relationships)
        if tok.startswith("role:"):
            return tok[5:] in self.roles
        return False

    def resolve(self, toks: list[str], *, speaker: str, about: set[str]) -> tuple[set[str], set[str], int]:
        """Tokens -> (principals, explicitly named principals, unknown token count)."""
        out: set[str] = set()
        explicit: set[str] = set()
        unknown = 0
        pool = self._pool() | {speaker}
        for raw in toks or []:
            tok = str(raw).strip()
            if tok == "speaker":
                out.add(speaker)
            elif tok == "about":
                out |= {a for a in about if a in self.principals}
            elif tok == "everyone":
                out |= pool
            elif tok in self.principals:
                out.add(tok)
                explicit.add(tok)
            elif tok in self.entities:
                for rel in self.relationships:
                    if tok in rel["entities"]:
                        out |= set(rel["principals"]) & pool
            elif tok.startswith("group:"):
                for rel in self.relationships:
                    if str(rel["id"]) == tok[6:]:
                        out |= set(rel["principals"]) & (pool | set(rel["principals"]) if self.active is None else pool)
                        break
                else:
                    unknown += 1
            elif tok.startswith("role:") and tok[5:] in self.roles:
                out |= {p for p, (r, _) in self.principals.items() if r == tok[5:] and p in pool}
            else:
                unknown += 1
        return out, explicit, unknown

    # ------------------------------------------------------------ aliases
    def _build_aliases(self) -> None:
        role_words = {w for r in self.roles for w in re.split(r"[_\W]+", r.lower()) if w}
        names: dict[str, set[str]] = {}
        single_counts: Counter[str] = Counter()
        per_target_tokens: dict[str, set[str]] = {}
        for pid, (_role, display) in self.principals.items():
            base = re.sub(r"\([^)]*\)", " ", display or "").strip()
            if base:
                names.setdefault(base.lower(), set()).add(pid)
            toks = {t for t in re.findall(r"[A-Za-z][A-Za-z'\-]+", base) if len(t) >= 3 and t.lower() not in role_words}
            per_target_tokens[pid] = toks
            single_counts.update({t.lower() for t in toks})
        id_tokens: Counter[str] = Counter()
        id_split = {i: [t for t in re.split(r"[_\W]+", i.lower()) if len(t) >= 3]
                    for i in list(self.principals) + self.entities}
        for toks in id_split.values():
            id_tokens.update(set(toks))
        self._multi: list[tuple[str, str]] = []      # case-insensitive full names
        self._single: list[tuple[str, str]] = []     # case-sensitive capitalised tokens
        for name, targets in names.items():
            if len(targets) == 1 and " " in name:
                self._multi.append((name, next(iter(targets))))
            elif len(targets) == 1:
                self._single.append((name.capitalize(), next(iter(targets))))
        for pid, toks in per_target_tokens.items():
            for t in toks:
                if single_counts[t.lower()] == 1:
                    self._single.append((t[0].upper() + t[1:], pid))
        for ident, toks in id_split.items():
            for t in toks:
                if id_tokens[t] == 1 and t not in role_words and ident in self.entities:
                    self._multi.append((t, ident))
        for alias, target, _src in self.s.aliases():
            self._multi.append((alias.lower(), target))
        self._multi = sorted(set(self._multi), key=lambda x: -len(x[0]))
        self._single = sorted(set(self._single), key=lambda x: -len(x[0]))

    def mentions(self, text: str) -> list[Mention]:
        """Word-boundary, longest match first; a nested shorter name never matches inside a longer one."""
        taken: list[tuple[int, int]] = []
        out: list[Mention] = []

        def free(a: int, b: int) -> bool:
            return all(b <= x or a >= y for x, y in taken)

        cands: list[tuple[int, str, str, int, int]] = []
        for alias, target in self._multi:
            for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", text, re.I):
                cands.append((m.end() - m.start(), alias, target, m.start(), m.end()))
        for alias, target in self._single:
            for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", text):
                cands.append((m.end() - m.start(), alias, target, m.start(), m.end()))
        for _n, alias, target, a, b in sorted(cands, key=lambda c: (-c[0], c[3])):
            if free(a, b):
                taken.append((a, b))
                out.append(Mention(alias, target, a, b))
        return sorted(out, key=lambda m: m.start)

    def mentioned_ids(self, text: str) -> list[str]:
        return list(dict.fromkeys(m.target for m in self.mentions(text)))
