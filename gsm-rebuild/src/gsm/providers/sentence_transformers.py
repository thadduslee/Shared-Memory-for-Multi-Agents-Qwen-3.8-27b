"""Local sentence-embedding provider (``sentence-transformers``, optional dependency).

The default model, ``sentence-transformers/all-MiniLM-L6-v2``, is the one the
GateMem baselines embed with. Vectors are L2-normalized, so cosine similarity
in pgvector equals the model's own similarity. Encoding is serialized with a
lock: one model instance is shared by every episode worker.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

from gsm.errors import ProviderError
from gsm.providers.base import ProviderMetrics

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class SentenceTransformerEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, *, device: str = "cpu", batch_size: int = 64) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderError("sentence-transformers is not installed (pip install 'gsm[embeddings]')") from exc
        self._model: Any = SentenceTransformer(model, device=device)
        getter = getattr(self._model, "get_embedding_dimension", None) or self._model.get_sentence_embedding_dimension
        dim = getter()
        if not dim:
            raise ProviderError(f"{model} does not report an embedding dimension")
        self.dim = int(dim)
        self.model_id = f"st:{model}"
        self.batch_size = batch_size
        self.metrics = ProviderMetrics()
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        started = time.perf_counter()
        try:
            with self._lock:
                vecs = self._model.encode(
                    list(texts),
                    batch_size=self.batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
        except Exception as exc:
            self.metrics.record(ok=False)
            raise ProviderError(f"embedding failed: {type(exc).__name__}") from exc
        self.metrics.record(ok=True, latency_ms=(time.perf_counter() - started) * 1000)
        return [[float(x) for x in v] for v in vecs]
