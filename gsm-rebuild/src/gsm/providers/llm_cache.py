"""Append-only JSONL cache for deterministic replays of model calls."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any


class JsonlCache:
    """Keyed by a hash of (model, prompt version, system, user). Failures are never cached."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    self._data[row["k"]] = row["v"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # e.g. a line cut short by an interrupted run

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def get(self, k: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get(k)

    def put(self, k: str, v: dict[str, Any]) -> None:
        with self._lock:
            self._data[k] = v
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"k": k, "v": v}) + "\n")
