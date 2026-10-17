"""The append-only release chain: every prompt set the loop ever trusted.

    runs/<run>/releases/HEAD          the current champion's number (text file)
    runs/<run>/releases/<n>/          prompts/<component>.md, delta.json,
                                      probe.json, scores.json, meta.json
    runs/<run>/rejected/<iter>/       the same files for a candidate the gate
                                      (or the curator's lint) turned down

WHY APPEND-ONLY. The old loop overwrote its workspace and "rolled back" by
copying files around; twice a rollback restored the wrong tree and the
scoreboard disagreed with the code that produced it. Here a release directory
is written once, under a temporary name, and renamed into place (atomic on one
filesystem); a number that exists is never written again; HEAD moves forward by
exactly one and is replaced atomically. Resuming a run is reading HEAD.

WHY KEEP REJECTIONS. The next reflector is shown them (so it does not retry a
mechanism the gate already refused) and they are the negative half of the
optimiser's record. A rejected candidate's `number` is the ITERATION that
produced it (rejections do not advance the chain).
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from contracts import PROMPT_FILES, Component, Release
from loop.jsonx import read_json, write_json


class ChainError(RuntimeError):
    pass


def read_prompts(prompts_dir: Path) -> dict[str, str]:
    """component -> text; a missing file is an empty prompt (the curator can
    still add bullets to it)."""
    out = {}
    for comp, fname in PROMPT_FILES.items():
        p = Path(prompts_dir) / fname
        out[comp.value] = p.read_text(encoding="utf-8") if p.is_file() else ""
    return out


def write_prompts(prompts_dir: Path, prompts: dict[str, str]) -> None:
    prompts_dir = Path(prompts_dir)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for comp, fname in PROMPT_FILES.items():
        (prompts_dir / fname).write_text(prompts.get(comp.value, ""), encoding="utf-8")


class ReleaseChain:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "releases"
        self.rejected_root = self.run_dir / "rejected"

    # ------------------------------------------------------------ reading

    def exists(self) -> bool:
        return (self.root / "HEAD").is_file()

    def head(self) -> int:
        try:
            return int((self.root / "HEAD").read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            raise ChainError(f"no readable HEAD under {self.root}") from exc

    def numbers(self) -> list[int]:
        if not self.root.is_dir():
            return []
        return sorted(int(p.name) for p in self.root.iterdir() if p.is_dir() and p.name.isdigit())

    def path(self, n: int) -> Path:
        return self.root / str(n)

    def prompts_dir(self, n: int) -> Path:
        return self.path(n) / "prompts"

    def prompts(self, n: int) -> dict[str, str]:
        return read_prompts(self.prompts_dir(n))

    def meta(self, n: int) -> dict[str, Any]:
        return read_json(self.path(n) / "meta.json", {}) or {}

    def load(self, n: int) -> Release:
        meta = self.meta(n)
        if not meta:
            raise ChainError(f"release {n} does not exist")
        return Release(
            number=int(meta["number"]), parent=meta.get("parent"), component_changed=meta.get("component_changed"),
            prompts=self.prompts(n), delta=read_json(self.path(n) / "delta.json"),
            probe=read_json(self.path(n) / "probe.json"), scores=read_json(self.path(n) / "scores.json"),
            accepted=bool(meta.get("accepted")), reason=str(meta.get("reason") or ""),
        )

    def rejections(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.rejected_root.is_dir():
            return []
        rows = []
        for p in self.rejected_root.iterdir():
            meta = read_json(p / "meta.json")
            if isinstance(meta, dict):
                rows.append(meta)
        rows.sort(key=lambda m: (int(m.get("iteration") or 0), str(m.get("created_at") or "")))
        return rows[-limit:] if limit else rows

    # ------------------------------------------------------------ writing

    @staticmethod
    def _write_release_dir(target: Path, release: Release, extra: dict[str, Any]) -> None:
        if target.exists():
            raise ChainError(f"{target} already exists; the chain is append-only")
        tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
        try:
            write_prompts(tmp / "prompts", release.prompts)
            write_json(tmp / "delta.json", release.delta)
            write_json(tmp / "probe.json", release.probe)
            write_json(tmp / "scores.json", release.scores)
            meta = {k: v for k, v in asdict(release).items() if k not in ("prompts", "delta", "probe", "scores")}
            write_json(tmp / "meta.json", meta | {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S")} | extra)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tmp, target)  # fails if target appeared meanwhile: still append-only
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    def _set_head(self, n: int) -> None:
        tmp = self.root / f".HEAD.tmp-{os.getpid()}"
        tmp.write_text(f"{n}\n", encoding="utf-8")
        os.replace(tmp, self.root / "HEAD")

    def init_seed(self, prompts: dict[str, str], *, reason: str = "seed", extra: dict[str, Any] | None = None
                  ) -> Release:
        if self.exists():
            raise ChainError(f"{self.root} already has a chain (HEAD={self.head()})")
        missing = [c.value for c in Component if c.value not in prompts]
        if missing:
            raise ChainError(f"seed prompts missing components: {missing}")
        seed = Release(number=0, parent=None, component_changed=None, prompts=dict(prompts), delta=None,
                       probe=None, scores=None, accepted=True, reason=reason)
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_release_dir(self.path(0), seed, {"iteration": 0} | (extra or {}))
        self._set_head(0)
        return seed

    def append(self, release: Release, *, extra: dict[str, Any] | None = None) -> Release:
        """Add an ACCEPTED release on top of HEAD and advance HEAD. The number
        and parent are assigned here, not trusted from the caller."""
        head = self.head()
        release.number, release.parent, release.accepted = head + 1, head, True
        self._write_release_dir(self.path(release.number), release, extra or {})
        self._set_head(release.number)
        return release

    def reject(self, release: Release, *, iteration: int, extra: dict[str, Any] | None = None) -> Path:
        """Record a rejected candidate. HEAD does not move."""
        release.accepted = False
        release.number = iteration
        release.parent = self.head()
        target = self.rejected_root / str(iteration)
        k = 1
        while target.exists():  # a resumed iteration that was rejected twice
            target = self.rejected_root / f"{iteration}.{k}"
            k += 1
        self._write_release_dir(target, release, {"iteration": iteration} | (extra or {}))
        return target
