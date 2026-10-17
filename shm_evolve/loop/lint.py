"""No dataset vocabulary in an evolvable prompt (DESIGN.md §2.8 invariant 8).

WHY. The outer loop is an optimiser over prompts, and the fastest way for an
optimiser to raise a score on a fixed benchmark is to write the benchmark into
the prompt: "a <role> asking about a <relationship> may see ...". That is a
policy table for one dataset wearing a prompt's clothes, and it is exactly what
the old loop's v1 template did to reach 0.82 (memory note task-template-no-seed).
So every curated prompt is checked against the words the data itself uses.

THE WORD COLLECTION is computed at runtime from `bench/data/*/episodes.jsonl`
(never copied into code or tests): principal roles, relationship types,
principal ids, display names (parenthetical qualifiers dropped), every `*_id`
value in a relationship row, and the domain directory names -- the same
collection as the inner loop's tests/test_vocabulary_lint.py. Identifiers with underscores also match in their spaced form
("a_b" and "a b"). Matching is whole-word and case-insensitive. The inner
loop's `tests/test_vocabulary_lint.py` should import `dataset_vocabulary` from
here so both sides police the same list.

What is NOT collected, deliberately: individual tokens of multi-word names, and
the sub-words of compound identifiers. Both would flag ordinary English ("will",
"case", "support") and make the lint a reason to write worse prompts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loop.jsonx import read_jsonl

_PAREN = re.compile(r"\s*\([^)]*\)")


@dataclass(frozen=True)
class Vocabulary:
    roles: frozenset[str] = frozenset()
    relationship_types: frozenset[str] = frozenset()
    principal_ids: frozenset[str] = frozenset()
    display_names: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    entity_ids: frozenset[str] = frozenset()     # `*_id` values in relationships (projects, households, ...)

    def _groups(self) -> tuple[frozenset[str], ...]:
        return (self.roles, self.relationship_types, self.principal_ids, self.display_names, self.domains,
                self.entity_ids)

    def terms(self) -> frozenset[str]:
        out: set[str] = set()
        for group in self._groups():
            for term in group:
                t = term.strip().lower()
                if len(t) < 2:
                    continue
                out.add(t)
                if "_" in t:
                    out.add(t.replace("_", " "))
        return frozenset(out)

    def merged(self, other: Vocabulary) -> Vocabulary:
        return Vocabulary(*(a | b for a, b in zip(self._groups(), other._groups(), strict=True)))


def dataset_vocabulary(data_root: Path | str) -> Vocabulary:
    """Collect the vocabulary from every `<data_root>/*/episodes.jsonl`."""
    return _vocab_cached(str(Path(data_root).resolve()))


@lru_cache(maxsize=8)
def _vocab_cached(root: str) -> Vocabulary:
    roles: set[str] = set()
    rtypes: set[str] = set()
    pids: set[str] = set()
    names: set[str] = set()
    domains: set[str] = set()
    entity_ids: set[str] = set()
    for path in sorted(Path(root).glob("*/episodes.jsonl")):
        domains.add(path.parent.name)
        for ep in read_jsonl(path):
            if ep.get("domain"):
                domains.add(str(ep["domain"]))
            ents = ep.get("entities") or {}
            for p in ents.get("principals") or []:
                if p.get("role"):
                    roles.add(str(p["role"]))
                if p.get("principal_id"):
                    pids.add(str(p["principal_id"]))
                name = _PAREN.sub("", str(p.get("display_name") or "")).strip()
                if name:
                    names.add(name)
            for r in ents.get("relationships") or []:
                if r.get("type"):
                    rtypes.add(str(r["type"]))
                entity_ids |= {v for k, v in r.items() if str(k).endswith("_id") and isinstance(v, str) and v}
            for t in ep.get("turns") or []:
                sp = t.get("speaker") or {}
                if sp.get("role"):
                    roles.add(str(sp["role"]))
                if sp.get("principal_id"):
                    pids.add(str(sp["principal_id"]))
    return Vocabulary(frozenset(roles), frozenset(rtypes), frozenset(pids), frozenset(names), frozenset(domains),
                      frozenset(entity_ids))


@lru_cache(maxsize=8)
def _pattern(terms: frozenset[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    # Longest first so "a b c" wins over "a b" in the reported hit.
    alts = sorted((re.escape(t).replace(r"\ ", r"\s+") for t in terms), key=len, reverse=True)
    return re.compile(r"(?<![\w])(?:" + "|".join(alts) + r")(?![\w])", re.IGNORECASE)


def find_dataset_words(text: str, vocab: Vocabulary) -> list[str]:
    pat = _pattern(vocab.terms())
    if pat is None:
        return []
    return sorted({m.group(0).lower() for m in pat.finditer(text or "")})


@dataclass
class LintResult:
    ok: bool
    size: int
    max_size: int
    dataset_words: list[str] = field(default_factory=list)       # all hits in the prompt
    new_dataset_words: list[str] = field(default_factory=list)   # hits the edit introduced
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "size": self.size, "max_size": self.max_size, "dataset_words": self.dataset_words,
                "new_dataset_words": self.new_dataset_words, "problems": self.problems}


_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def lint_prompt(text: str, *, vocab: Vocabulary, max_chars: int, previous: str | None = None) -> LintResult:
    """Size, vocabulary, structure.

    Vocabulary hits already present in `previous` (the champion) are reported
    but do not fail the lint: the seed prompts are the inner loop's to keep
    clean, and blocking every candidate on a pre-existing hit would freeze the
    loop on someone else's bug. A hit the edit INTRODUCED always fails.

    "Parses" for a Markdown prompt means: still has a bullet, fences balanced,
    and the set of `{placeholder}` names unchanged (the memory system may
    `str.format` its prompts; a stray brace would crash it at runtime).
    """
    problems: list[str] = []
    words = find_dataset_words(text, vocab)
    prev_words = set(find_dataset_words(previous, vocab)) if previous is not None else set()
    new_words = [w for w in words if w not in prev_words]
    if new_words:
        problems.append(f"dataset vocabulary introduced: {new_words[:8]}")
    if len(text) > max_chars:
        problems.append(f"prompt is {len(text)} chars > MAX_PROMPT_CHARS={max_chars}")
    if not any(line.lstrip().startswith("- ") for line in text.splitlines()):
        problems.append("prompt has no bullet lines")
    if text.count("```") % 2:
        problems.append("unbalanced code fence")
    if previous is not None and set(_PLACEHOLDER.findall(text)) != set(_PLACEHOLDER.findall(previous)):
        problems.append("placeholder set changed")
    return LintResult(ok=not problems, size=len(text), max_size=max_chars, dataset_words=words,
                      new_dataset_words=new_words, problems=problems)


def lint_files(paths: Iterable[Path], vocab: Vocabulary) -> dict[str, list[str]]:
    """Vocabulary hits per file (for the optimiser's own prompts)."""
    return {str(p): hits for p in paths if (hits := find_dataset_words(Path(p).read_text(encoding="utf-8"), vocab))}
