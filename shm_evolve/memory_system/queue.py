"""The queue between the front door and the processor.  (not prompted)

It exists so the processor can see several chunks at once -- a fact and its
correction, two speakers stating one thing -- and for nothing else: it is
flushed before every query, so it can never make a query see stale state.
Order is (timestamp, turn_id), stable within a turn.

`hold` lets the processor defer a chunk to the next flush (it expects a
later turn to settle it), at most HOLD_MAX times; after that the chunk is
processed with hold forbidden. Flushing an empty queue does nothing at all
-- no processor call, no write -- which is what makes `query`'s flush
idempotent and lets `query` leave the database byte-identical.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from memory_system.ops import Chunk


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


HOLD_MAX = _env_int("MEMORY_HOLD_MAX", 2)
QUEUE_BATCH = _env_int("MEMORY_QUEUE_BATCH", 1)


@dataclass
class _Entry:
    chunk: Chunk
    order: int
    holds: int = 0


@dataclass
class ChunkQueue:
    batch: int = QUEUE_BATCH
    hold_max: int = HOLD_MAX
    _items: list[_Entry] = field(default_factory=list)
    _n: int = 0

    def __len__(self) -> int:
        return len(self._items)

    def push(self, chunk: Chunk) -> None:
        self._n += 1
        self._items.append(_Entry(chunk, self._n))
        self._items.sort(key=lambda e: (e.chunk.timestamp, e.chunk.turn_id, e.order))

    def pending(self) -> list[Chunk]:
        return [e.chunk for e in self._items]

    def ready(self) -> bool:
        return len(self._items) >= self.batch

    def flush(self, process: Callable[[list[Chunk], bool], set[str]], *, final: bool = False) -> int:
        """Run `process(chunks, allow_hold)` over everything pending. `process`
        returns the chunk ids it held. With `final`, nothing may be held.
        Returns the number of chunks taken off the queue."""
        if not self._items:
            return 0
        batch = list(self._items)
        allow_hold = not final and all(e.holds < self.hold_max for e in batch)
        held = process([e.chunk for e in batch], allow_hold) if batch else set()
        keep: list[_Entry] = []
        for e in batch:
            if allow_hold and e.chunk.chunk_id in held:
                e.holds += 1
                keep.append(e)
        self._items = keep
        return len(batch) - len(keep)

    def clear(self) -> None:
        self._items.clear()
