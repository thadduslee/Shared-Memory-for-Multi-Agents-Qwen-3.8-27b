"""The shard runner must never be exec'd while another shard is writing it.

Every shard of an iteration shares ONE workspace and each wrote `_eval_runner.py`
before exec'ing it. `write_text` truncates first, so a shard that started while
another was mid-write ran an EMPTY script: exit 0, no stdout, no stderr, and a
"produced no result" failure carrying no diagnosis. A 50-checkpoint slice is 5
shards and never showed it; the first 579-checkpoint run is 58 shards and lost
shards 17 and 27, i.e. 20 checkpoints charged against U as missing predictions.
"""

from __future__ import annotations

import threading
from pathlib import Path

from nodes.medical_evaluator import _EVAL_RUNNER, _materialise_runner


def _hammer(target: Path, writer, threads: int = 32, reads: int = 10) -> list[int]:
    """Run `writer` from many threads; collect the size of every bad read."""
    bad: list[int] = []

    def worker() -> None:
        for _ in range(reads):
            path = writer(target)
            text = path.read_text(encoding="utf-8")
            if text != _EVAL_RUNNER:
                bad.append(len(text))

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return bad


def test_concurrent_shards_never_see_a_partial_runner(tmp_path: Path) -> None:
    assert _hammer(tmp_path, _materialise_runner) == []
    assert (tmp_path / "_eval_runner.py").read_text(encoding="utf-8") == _EVAL_RUNNER


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    _hammer(tmp_path, _materialise_runner)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_the_naive_write_really_does_race(tmp_path: Path) -> None:
    """Guards the guard: if this ever stops racing, the test above proves nothing."""

    def naive(workspace: Path) -> Path:
        path = workspace / "_eval_runner.py"
        path.write_text(_EVAL_RUNNER, encoding="utf-8")
        return path

    assert _hammer(tmp_path, naive), "expected the truncate-then-write path to race"
