"""Relevance: keyed-hash lexical coverage plus embedding cosine. Reads no plaintext."""

from __future__ import annotations

import math

import numpy as np

from govmem.core.store import Store
from govmem.embed import Embedder
from govmem.text import tokens


class Relevance:
    def __init__(self, store: Store, embedder: Embedder) -> None:
        self.s = store
        self.e = embedder

    def score(self, query: str, rids: list[str], *, lexical_weight: float = 0.5) -> dict[str, float]:
        if not rids:
            return {}
        words = list(dict.fromkeys(tokens(query)))
        lex: dict[str, float] = {r: 0.0 for r in rids}
        if words:
            hashes = self.s.term_hashes(words)
            hmap = dict(zip(hashes, words))
            df: dict[str, int] = {h: 0 for h in hashes}
            hits: dict[str, set[str]] = {r: set() for r in rids}
            rid_set = set(rids)
            for i in range(0, len(hashes), 400):
                hc = hashes[i:i + 400]
                marks = ",".join("?" * len(hc))
                for row in self.s.q(f"SELECT record_id, term_hash FROM terms WHERE term_hash IN ({marks})", tuple(hc)):
                    if row["record_id"] in rid_set:
                        df[row["term_hash"]] += 1
                        hits[row["record_id"]].add(row["term_hash"])
            n = len(rids)
            idf = {h: math.log(1 + (n + 1) / (df[h] + 0.5)) for h in hashes}
            total = sum(idf.values()) or 1.0
            for r in rids:
                lex[r] = sum(idf[h] for h in hits[r]) / total
            del hmap
        qv = self.e.encode([query])[0]
        embs = self.s.embeddings(rids)
        out: dict[str, float] = {}
        for r in rids:
            v = embs.get(r)
            cos = float(np.dot(qv, v)) if v is not None and v.shape == qv.shape else 0.0
            out[r] = lexical_weight * lex[r] + (1 - lexical_weight) * max(cos, 0.0)
        return out

    def rank(self, query: str, rids: list[str], k: int) -> list[tuple[str, float]]:
        sc = self.score(query, rids)
        return sorted(sc.items(), key=lambda kv: -kv[1])[:k]
