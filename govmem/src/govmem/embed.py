"""Sentence embeddings: a pretrained encoder used as-is, with a hashing fallback so lexical-only still works."""

from __future__ import annotations

import hashlib
import os
import threading

import numpy as np

from govmem.text import tokens

DIM_FALLBACK = 384


class Embedder:
    _shared: "Embedder | None" = None
    _lock = threading.Lock()

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.environ.get("GOVMEM_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        self._model = None
        self.backend = "hash"
        if os.environ.get("GOVMEM_EMBED_BACKEND", "st") == "st":
            try:
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name, device="cpu")
                self.backend = "st"
            except Exception:  # noqa: BLE001 -- any failure falls back to hashing
                self._model = None

    @classmethod
    def shared(cls) -> "Embedder":
        with cls._lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM_FALLBACK), dtype=np.float32)
        if self._model is not None:
            with self._lock:
                v = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            return np.asarray(v, dtype=np.float32)
        return np.stack([self._hash(t) for t in texts])

    @staticmethod
    def _hash(text: str) -> np.ndarray:
        v = np.zeros(DIM_FALLBACK, dtype=np.float32)
        for t in tokens(text):
            h = int(hashlib.md5(t.encode()).hexdigest(), 16)
            v[h % DIM_FALLBACK] += 1.0 if (h >> 8) & 1 else -1.0
        n = float(np.linalg.norm(v))
        return v / n if n else v


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)
