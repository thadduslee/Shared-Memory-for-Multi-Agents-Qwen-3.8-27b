"""Method side of CIMemories: write one message per (context, trial) into ``solutions.jsonl``.

A method gets a sanitized profile (memory statements under opaque ids) and one context, and returns the
message it would send to ``recipient`` for ``task``. It never sees labels, attribute keys or the reveal judge.

``FullContextMethod`` is the paper's setup: every memory statement in the prompt, upstream's
``task_solving`` prompt at a chosen privacy level (1 = no privacy instruction ... 4). A GSM method plugs in
behind the same interface.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml  # type: ignore[import-untyped]

from gsm.errors import ProviderError
from gsm.eval.cimemories.data import DEFAULT_PROMPTS, MethodContext, MethodMemory, MethodProfile
from gsm.providers.openai_compat import OpenAICompatClient

log = logging.getLogger(__name__)


class CIMemoriesMethod(Protocol):
    name: str

    def respond(self, profile: MethodProfile, context: MethodContext, trial: int) -> dict[str, Any]:
        """``{"message": str, "memory_ids_used": [...] | None, ...}``; extra keys are kept as diagnostics."""
        ...


def load_prompts(path: Path = DEFAULT_PROMPTS) -> dict[str, Any]:
    prompts: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return prompts


def task_prompt(template: str, memories: tuple[MethodMemory, ...], context: MethodContext) -> str:
    # Upstream lower-cases the recipient and joins statements with newlines (eval.py, Stage A).
    return template.format(
        task=context.task, recipient=context.recipient.lower(), memories="\n".join(m.statement for m in memories)
    ).strip()


@dataclass
class FullContextMethod:
    client: OpenAICompatClient
    privacy_level: int = 1
    temperature: float | None = None  # upstream default: non-greedy
    prompts: dict[str, Any] = field(default_factory=load_prompts)

    @property
    def name(self) -> str:
        return f"full_context_l{self.privacy_level}"

    def respond(self, profile: MethodProfile, context: MethodContext, trial: int) -> dict[str, Any]:
        prompt = task_prompt(self.prompts["task_solving"][self.privacy_level], profile.memories, context)
        (text,) = self.client.sample(prompt, n=1, temperature=self.temperature)
        return {"message": text.strip(), "memory_ids_used": [m.memory_id for m in profile.memories]}


MAX_FAILED_JOBS = 8  # failed jobs in a row before the run stops (quota exhausted, endpoint down)


def run_method(
    method: CIMemoriesMethod,
    profiles: list[MethodProfile],
    out_dir: Path,
    *,
    trials: int = 5,
    workers: int = 16,
    on_done: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Resumable: (context_id, trial) pairs already in ``solutions.jsonl`` are skipped.

    Only successful answers are written, so a run cut short (provider quota, Ctrl-C, SIGTERM) is continued
    by running it again. After ``MAX_FAILED_JOBS`` failures in a row it stops and reports ``stopped_early``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "solutions.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:  # a line torn by a kill
                continue
            done.add((row["context_id"], row["trial"]))
    jobs = [(p, c, t) for p in profiles for c in p.contexts for t in range(trials) if (c.context_id, t) not in done]
    stop = threading.Event()

    def one(job: tuple[MethodProfile, MethodContext, int]) -> dict[str, Any] | None:
        if stop.is_set():
            return None
        p, c, t = job
        try:
            out = method.respond(p, c, t)
        except ProviderError as exc:
            log.warning("answer failed for %s trial %d: %s", c.context_id, t, exc)
            return None
        return {"profile_id": p.profile_id, "context_id": c.context_id, "trial": t, **out}

    written = failed = failed_in_a_row = 0
    interrupted = False
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [pool.submit(one, job) for job in jobs]
    try:
        with path.open("a", encoding="utf-8") as fh:
            for fut in as_completed(futures):
                row = fut.result()
                if row is None:
                    failed += 1
                    failed_in_a_row += 1
                    if failed_in_a_row >= MAX_FAILED_JOBS and not stop.is_set():
                        log.error("%d answers in a row failed; stopping", failed_in_a_row)
                        stop.set()
                    continue
                failed_in_a_row = 0
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                written += 1
                if on_done:
                    on_done(written, len(jobs))
    except KeyboardInterrupt:
        interrupted = True
        log.error("interrupted; answers written so far are kept")
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
    summary = {
        "method": method.name,
        "profiles": len(profiles),
        "trials": trials,
        "already_done": len(done),
        "new_solutions": written,
        "remaining": len(jobs) - written,
        "stopped_early": bool(interrupted or len(jobs) - written),
        **getattr(method, "stats", {}),
    }
    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary
