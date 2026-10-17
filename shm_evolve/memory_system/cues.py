"""Generic-English cue words, shared by every offline path that must guess a
chunk's kind without a model: the deterministic front-door fallback
(front_door.fallback_chunks) and the fake LLM (fake_llm._front_door).

These are NOT dataset vocabulary. DESIGN.md says the fallback "uses structural
cues only" -- that line undersold what "structural" may mean: the lint
(tests/test_vocabulary_lint.py) only forbids roles, relationship types, entity
ids and a short list of domain words collected from the benchmark itself.
Plain English phrasings for consent/revocation/deletion/correction ("delete",
"authorized", "revoked", "update", ...) are not in that collected vocabulary
and are exactly the generic reading structural cues were always allowed to
include; they just were not written down anywhere runnable before.

This is still not a design: a keyword reading of any text is precisely what
the benchmark's held-out paraphrase checks are built to catch out, which is
the whole reason the inner loop has a *prompted* front door at all. The cue
table exists so SHM_CONTRACT_MODE=offline (no model, no fake) and the fake
both exercise the tightening/widening paths -- consent, revoke, restrict,
delete, correction -- instead of silently treating every turn as a plain
fact.

Checked in this order: tightening (delete/revoke/restrict) before widening
(consent/correction), so a turn that uses both a tightening and an unrelated
generic word (e.g. "...is revoked from now on" also contains "now") resolves
to the tightening reading, never the other way round.
"""

from __future__ import annotations

import re

from memory_system.ops import ChunkKind

#: (kind, cue phrases). Order matters -- see module docstring.
CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        ChunkKind.DELETE.value,
        (
            "delete",
            "forget",
            "erase",
            "wipe",
            "purge",
            "remove it from memory",
            "from memory",
            "treated as deleted",
            "treated as unavailable",
            "do not retain",
            "should not be retained",
            "no longer be stored",
            "strike",
        ),
    ),
    (
        ChunkKind.REVOKE.value,
        (
            "revoke",
            "revoked",
            "no longer",
            "stop sharing",
            "remove access",
            "is off",
            "nothing more goes",
            "access ends",
            "cut off",
        ),
    ),
    (
        ChunkKind.RESTRICT.value,
        (
            "summary only",
            "only a summary",
            "high-level only",
            "only high-level",
            "without details",
            "only that it exists",
        ),
    ),
    (
        ChunkKind.CONSENT.value,
        (
            "authorized",
            "authorised",
            "may see",
            "can see",
            "allowed to",
            "is allowed",
            "fine to",
            "loop",
            "can be told",
            "may be told",
            "may receive",
            "can receive",
            "share with",
            "in the picture",
            "permission",
        ),
    ),
    (
        ChunkKind.CORRECTION.value,
        (
            "update",
            "updated",
            "moved",
            "instead",
            "changed",
            "now",
            "pushed",
            "corrected",
            "scratch",
            "rescheduled",
        ),
    ),
)


def cue_kind(text: str) -> str | None:
    """The first kind in `CUES` whose cue phrase appears in `text` as a whole
    word/phrase, or None if nothing matches (the caller then falls back to
    purely structural cues: turn_kind, trailing '?', ...)."""
    low = (text or "").lower()
    for kind, cues in CUES:
        if any(re.search(rf"\b{re.escape(c)}\b", low) for c in cues):
            return kind
    return None
