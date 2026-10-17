"""Label-free calibration of the vector-similarity thresholds for a sentence embedder.

Uses only dialogue turns (no checkpoints, no annotations). Sentences from
*different* episodes are unrelated by construction (identical boilerplate
sentences excluded), so their similarities are the embedder's noise floor for
this domain's vocabulary and style:

* questions asked in the dialogue stand in for queries, statements for stored facts;
* ``vector_min_similarity`` (a vector match alone is relevant) is a high quantile
  of all cross-episode question/statement similarities;
* ``vector_support_similarity`` (what a lexical match must also reach) is a low
  quantile of the cross-episode pairs that share a content word: coincidental
  lexical matches, the kind the support floor exists to reject.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from gsm.disclosure.schemas import normalize
from gsm.eval.gatemem.sanitize import MethodEpisode
from gsm.providers.base import Embedder
from gsm.retrieval.planner import tokenize

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Calibration:
    vector_min_similarity: float
    vector_support_similarity: float
    pairs: int
    overlapping_pairs: int
    quantiles: dict[str, float]
    overlapping_quantiles: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _sentences(ep: MethodEpisode) -> tuple[list[str], list[str]]:
    questions: list[str] = []
    statements: list[str] = []
    for t in ep.turns:
        for s in _SENTENCE.split(t.text.strip()):
            s = s.strip()
            if len(tokenize(s)) < 3:
                continue
            (questions if s.endswith("?") else statements).append(s)
    return questions, statements


def _quantile(xs: Sequence[float], q: float) -> float:
    ys = sorted(xs)
    return ys[min(len(ys) - 1, max(0, round(q * (len(ys) - 1))))] if ys else 0.0


def calibrate(
    episodes: Sequence[MethodEpisode],
    embedder: Embedder,
    *,
    high: float = 0.99,
    low: float = 0.25,
    max_questions: int = 400,
    max_statements: int = 2000,
    seed: int = 7,
) -> Calibration:
    rng = random.Random(seed)  # noqa: S311 - sampling, not security
    qs: list[tuple[int, str]] = []
    ss: list[tuple[int, str]] = []
    for i, ep in enumerate(episodes):
        q, s = _sentences(ep)
        qs += [(i, x) for x in q]
        ss += [(i, x) for x in s]
    if len(qs) < 20:  # few questions in the dialogue: statements stand in for queries
        qs = qs + rng.sample(ss, min(len(ss), max_questions))
    qs = rng.sample(qs, min(len(qs), max_questions))
    ss = rng.sample(ss, min(len(ss), max_statements))
    qv = embedder.embed([x for _, x in qs])
    sv = embedder.embed([x for _, x in ss])
    norm_q = [normalize(x) for _, x in qs]
    norm_s = [normalize(x) for _, x in ss]
    q_tokens = [set(tokenize(x)) for _, x in qs]
    s_tokens = [set(tokenize(x)) for _, x in ss]
    sims: list[float] = []
    overlapping: list[float] = []
    for a, (ea, _) in enumerate(qs):
        for b, (eb, _) in enumerate(ss):
            if ea == eb or norm_q[a] == norm_s[b]:
                continue  # same episode (possibly related) or the same boilerplate sentence
            sim = sum(x * y for x, y in zip(qv[a], sv[b], strict=True))
            sims.append(sim)
            if q_tokens[a] & s_tokens[b]:
                overlapping.append(sim)
    marks = (0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 0.999)
    return Calibration(
        vector_min_similarity=round(_quantile(sims, high), 3),
        vector_support_similarity=round(_quantile(overlapping, low), 3),
        pairs=len(sims),
        overlapping_pairs=len(overlapping),
        quantiles={f"p{m * 100:g}": round(_quantile(sims, m), 3) for m in marks},
        overlapping_quantiles={f"p{m * 100:g}": round(_quantile(overlapping, m), 3) for m in marks},
    )
