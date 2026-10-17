"""DESIGN.md §2.8.8: no dataset vocabulary in the memory system's code or prompts.

The vocabulary is collected from the benchmark itself -- every role,
relationship type, principal id, display name and `*_id` value in
bench/data/*/episodes.jsonl, plus the domain names -- so the lint grows with
the data instead of trusting a hand-made list. Matching is whole-word and
case-insensitive. The prompts are additionally checked against a short list
of generic domain words: a prompt that says "the patient" has been fitted to
a dataset even if no id appears in it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "memory_system"
DATA = ROOT.parent / "bench" / "data"

GENERIC_DOMAIN_WORDS = (
    "patient",
    "clinician",
    "doctor",
    "nurse",
    "project",
    "household",
    "student",
    "professor",
    "employee",
    "manager",
    "contractor",
    "lab",
    "prescription",
    "budget",
    "vendor",
    "tenant",
)


def _collect() -> set[str]:
    vocab: set[str] = set()
    for path in sorted(DATA.glob("*/episodes.jsonl")):
        vocab.add(path.parent.name)
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                ep = json.loads(line)
                if ep.get("domain"):
                    vocab.add(str(ep["domain"]))
                ents = ep.get("entities") or {}
                for p in ents.get("principals") or []:
                    vocab |= {str(p.get(k) or "") for k in ("principal_id", "role", "display_name")}
                for rel in ents.get("relationships") or []:
                    vocab.add(str(rel.get("type") or ""))
                    for k, v in rel.items():
                        if k.endswith("_id") and isinstance(v, str):
                            vocab.add(v)
    return {v.strip() for v in vocab if v and v.strip()}


#: Copied verbatim and frozen (the brief forbids editing them); llm.py's
#: docstring says "the agent", which is also a role name in one dataset.
FROZEN = frozenset({"keyring.py", "llm.py"})


def _sources() -> list[Path]:
    code = [p for p in sorted(MEMORY.glob("*.py")) if p.name not in FROZEN]
    return code + sorted((MEMORY / "prompts").glob("*.md"))


def _hits(text: str, words: set[str]) -> list[str]:
    low = text.lower()
    tokens = set(re.findall(r"[a-z0-9_]+", low))
    found = []
    for w in words:
        lw = w.lower()
        if re.fullmatch(r"[a-z0-9_]+", lw):
            hit = lw in tokens
        else:
            hit = re.search(rf"(?<![a-z0-9_]){re.escape(lw)}(?![a-z0-9_])", low) is not None
        if hit:
            found.append(w)
    return sorted(found)


@pytest.fixture(scope="module")
def vocabulary() -> set[str]:
    if not DATA.is_dir():
        pytest.skip(f"benchmark data not found at {DATA}")
    vocab = _collect()
    assert len(vocab) > 100, "the collector found suspiciously little vocabulary"
    return vocab


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_dataset_vocabulary(path: Path, vocabulary: set[str]) -> None:
    hits = _hits(path.read_text(encoding="utf-8"), vocabulary)
    assert not hits, f"{path.name} contains dataset vocabulary: {hits}"


@pytest.mark.parametrize("path", sorted((MEMORY / "prompts").glob("*.md")), ids=lambda p: p.name)
def test_prompts_have_no_domain_words(path: Path) -> None:
    hits = _hits(path.read_text(encoding="utf-8"), set(GENERIC_DOMAIN_WORDS))
    assert not hits, f"{path.name} contains domain words: {hits}"


def test_every_component_has_a_prompt_with_its_header() -> None:
    from contracts import Component

    for c in Component:
        text = (MEMORY / "prompts" / f"{c.value}.md").read_text(encoding="utf-8")
        assert text.splitlines()[0].strip() == f"# component: {c.value}"


def test_the_lint_would_catch_a_planted_word(vocabulary: set[str]) -> None:
    role = sorted(v for v in vocabulary if re.fullmatch(r"[a-z_]+", v))[0]
    assert _hits(f"if x == '{role}':", vocabulary)
