"""Word terms, and the blind index that lets SQL match them.

Bodies are sealed, so SQL cannot see their words. Every path that needs
"records that share words with this text" -- the retriever's candidates, the
processor's peek, the librarian's deletion matching -- goes through the terms
defined here. At insert time the envelope stores, per record, a keyed hash of
each term (`_terms`); a search hashes its own terms with the same key and
joins. That keeps candidate selection in SQL (a 250-turn episode is queried
~28 times; opening every record per query would be a full scan in Python) and
keeps plaintext off disk: the hashes are keyed with a per-store secret and are
deleted with the record when it is tombstoned.

The stopword list is English function words only. It must stay free of any
dataset vocabulary: it says nothing about what a memory is about, only which
words never discriminate.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from collections.abc import Iterable

STOPWORDS = frozenset(
    """
a about above after again all also am an and any are as at be been before being below between both
but by can could did do does doing done down during each either else ever every few for from further
get gets got had has have having he her here hers him his how i if in into is it its itself just
let me might more most much must my no nor not now of off on once only or other our ours out over
own per please same shall she should so some such than that the their theirs them then there these
they this those through to too under until up upon us very via was we were what when where which
while who whom whose why will with would yes yet you your yours ok okay
""".split()  # noqa: SIM905 -- a word list reads better as text
)

_WORD = re.compile(r"[a-z0-9][a-z0-9_]*")


def words(text: str) -> list[str]:
    """Lower-cased word tokens. Hyphens split (a code like `abc-123` yields
    both halves); underscores do not (declared ids keep their shape)."""
    return _WORD.findall((text or "").lower())


def stem(word: str) -> str:
    """A light suffix strip so 'scheduled' meets 'schedule'. Deliberately
    crude: it only has to make two spellings of one word collide more often
    than two different words do."""
    if word.isdigit() or "_" in word or len(word) <= 4:
        return word
    for suffix in ("ations", "ation", "ings", "ing", "edly", "ed", "ies", "es", "ly", "s"):
        if suffix == "s" and word[-2:] in ("ss", "us", "is"):
            continue
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word.rstrip("e") if len(word) > 5 else word


def terms(text: str) -> set[str]:
    """The discriminating terms of a text."""
    return {stem(w) for w in words(text) if w not in STOPWORDS and (len(w) >= 2 or w.isdigit())}


def blind(key: bytes, term: str) -> str:
    """Keyed hash of one term, as stored in `_terms`."""
    return hmac.new(key, term.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def blind_all(key: bytes, items: Iterable[str]) -> list[str]:
    return sorted({blind(key, t) for t in items})


def idf(n_docs: int, df: int) -> float:
    """Smoothed inverse document frequency; an unseen term weighs the most,
    which is what makes 'the thing you asked about is not here' detectable."""
    return math.log(1.0 + n_docs / (1.0 + df)) if n_docs > 0 else 1.0
