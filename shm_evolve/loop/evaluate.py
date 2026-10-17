"""One pass per episode, concurrently, with the field wall enforced.

THE PROTOCOL (DESIGN.md §3.1). For each episode: `reset`, then `ingest` each
turn in order, and right after the turn whose id equals a checkpoint's
`as_of_turn_id`, `query` that checkpoint. A checkpoint whose as_of sorts before
the first turn is asked straight after `reset`; one that matches no turn and
does not sort first is asked after the last turn and counted as `unplaced`
(data.place_checkpoints). The old evaluator replayed the episode prefix once
per checkpoint -- ~28x the ingest work and ~28x the memory model's calls -- and
leaned on the model cache to survive it; one pass is what the memory would see
in deployment.

WHY A PROCESS POOL (`spawn`). Each episode owns one SQLite file under the
iteration dir and one process: the memory's model cache, its budget counters
and anything its modules keep at import are per-process, and a fresh `spawn`
child guarantees the agent module is imported IN the child with the prompts of
THIS evaluation (a module that reads its prompt at import would otherwise keep
the champion's text when the candidate runs). The agent class is given as an
import path ("pkg.module:Class") so the real `GateMemAgent` drops in for the
test stub without touching this file.

THE WALL. The child receives the visible episode and the path of
`checkpoints.stripped.jsonl`, nothing else. An audit hook in the child refuses
to `open` any annotated `checkpoints.jsonl` under the data root, so even a
memory system that goes looking cannot read the labels; every checkpoint is
also checked with `assert_no_hidden_fields` before `query`.

WHY THE ANSWERER RUNS IN THE PARENT. The answer render is the metered,
GPU-bound half; it must share ONE semaphore across all episodes, which only the
parent's event loop can hold. Children return evidence; the parent renders each
episode's checkpoints as soon as that episode finishes, while slower episodes
are still ingesting.

REUSE. An episode's result is keyed by the hash of the four prompts plus the
agent path. `reuse_from` lets the gate take the champion's probe results out
of its full evaluation and the candidate's full evaluation out of its probe,
instead of paying for the same episode twice.

TRACES. The agent's trace is read once per episode, after the last query:
`agent.trace_events()` if it exists (the stub, and the interface asked of
GateMemAgent), else the `trace` table in the episode's SQLite file.
`episode_id` is added at serialisation; `contracts.TraceEvent` is unchanged.

TIMEOUT. Each episode future is bounded by `cfg.episode_timeout_s`
(`EPISODE_TIMEOUT_S`, default 3600): a hung child -- a wedged endpoint, a
retry loop that never gives up -- stops the PARENT waiting on it, so the rest
of the evaluation proceeds and the episode is recorded in `episode_errors`
with reason "timeout". The child process itself cannot be interrupted once it
is running (`ProcessPoolExecutor` offers no preemption); it is left to finish
or hit its own `MEMORY_LLM_CALL_BUDGET`, and the pool's shutdown at the end of
`evaluate()` still waits for it, same as any other worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import multiprocessing
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from contracts import PROMPT_FILES, TraceEvent
from loop import config as config_mod
from loop.answerer import render_answer
from loop.config import Config
from loop.data import (
    Dataset,
    assert_no_hidden_fields,
    make_prediction_row,
    place_checkpoints,
    requester_relationships,
    strip_hidden_fields,
    visible_checkpoint,
)
from loop.jsonx import read_json, read_jsonl, write_json, write_jsonl
from loop.llm_client import LLMClientProtocol

log = logging.getLogger("loop.evaluate")

STRIPPED_NAME = "checkpoints.stripped.jsonl"
ANNOTATED_NAME = "checkpoints.jsonl"
_TRACE_FIELDS = set(TraceEvent.__dataclass_fields__)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120] or "_"


def prompts_hash(prompts_dir: Path, agent_path: str) -> str:
    """The key under which an episode's result may be reused.

    It covers everything that changes what the memory does: the four
    prompts, the agent's import path, the memory_system source itself, and
    the memory model's thinking configuration. The first two alone let a
    result computed under different code or a different reasoning setting
    be reused as if nothing had changed (noticed on the first joint run).
    """
    h = hashlib.sha256(agent_path.encode())
    for comp_file in sorted(PROMPT_FILES.values()):
        p = Path(prompts_dir) / comp_file
        h.update(comp_file.encode() + b"\0" + (p.read_bytes() if p.is_file() else b"<missing>") + b"\0")
    src_dir = Path(__file__).resolve().parent.parent / "memory_system"
    for src in sorted(src_dir.glob("*.py")):
        h.update(src.name.encode() + b"\0" + src.read_bytes() + b"\0")
    fixed = ("MEMORY_LLM_THINKING", "MEMORY_LLM_THINKING_COMPONENTS", "MEMORY_LLM_THINKING_MAX_TOKENS",
             "MEMORY_LLM_MODEL")
    # Per-component reasoning caps (MEMORY_LLM_THINKING_MAX_TOKENS_<COMPONENT>) change
    # what the memory does just as much as the shared cap does.
    per_component = sorted(k for k in os.environ if k.startswith("MEMORY_LLM_THINKING_MAX_TOKENS_"))
    for var in (*fixed, *per_component):
        h.update(var.encode() + b"=" + os.environ.get(var, "").encode() + b"\0")
    return h.hexdigest()[:20]


# =====================================================================
# Child side. Everything below until `evaluate` runs in the episode process.
# =====================================================================

_FORBIDDEN: set[str] = set()
_WALL_INSTALLED = False


def _wall_hook(event: str, args: tuple[Any, ...]) -> None:
    if event != "open" or not args or not _FORBIDDEN:
        return
    target = args[0]
    if isinstance(target, int) or target is None:
        return
    try:
        path = os.fsdecode(target)
    except TypeError:
        return
    # Cheap test first: the audit hook sees every open in the process.
    if os.path.basename(path) != ANNOTATED_NAME:
        return
    if os.path.realpath(path) in _FORBIDDEN:
        raise PermissionError(f"field wall: an episode process may not open annotated {path}")


def _install_wall(forbidden: list[str]) -> None:
    global _WALL_INSTALLED
    _FORBIDDEN.clear()
    _FORBIDDEN.update(os.path.realpath(p) for p in forbidden)
    if not _WALL_INSTALLED:
        sys.addaudithook(_wall_hook)  # cannot be removed; fine in a pool worker
        _WALL_INSTALLED = True


def _install_memory_fake() -> str:
    """The memory side's fake, if the inner loop ships one. Without it, blank
    the endpoint so the frozen llm.py returns None (its contract path) instead
    of reaching for a server that is not there."""
    try:
        mod = importlib.import_module("memory_system.fake_llm")
    except ImportError:
        os.environ["MEMORY_LLM_BASE_URL"] = ""
        return "absent"
    install = getattr(mod, "install", None)
    if callable(install):
        install()
        return "installed"
    os.environ["MEMORY_LLM_BASE_URL"] = ""
    return "no-install"


def _import_agent(path: str) -> type:
    module_name, _, attr = path.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr or "GateMemAgent")


def _construct(cls: type, db_path: str, prompts_dir: str) -> Any:
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        params = {}
    if "prompts_dir" in params:
        return cls(db_path, prompts_dir=prompts_dir)
    return cls(db_path)


def _event_dict(ev: Any) -> dict[str, Any]:
    if isinstance(ev, TraceEvent):
        return asdict(ev)
    if isinstance(ev, str):
        ev = json.loads(ev)
    out = {k: v for k, v in dict(ev).items() if k in _TRACE_FIELDS}
    for k in ("op_args", "record_ids", "denied_ids"):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except json.JSONDecodeError:
                out[k] = {} if k == "op_args" else []
    if "llm_called" in out:
        out["llm_called"] = bool(out["llm_called"])
    return asdict(TraceEvent(**{**{"component": "", "turn_id": "", "op": "", "outcome": ""}, **out}))


def _drain_trace(agent: Any, db_path: str) -> tuple[list[dict[str, Any]], str]:
    fn = getattr(agent, "trace_events", None)
    if callable(fn):
        return [_event_dict(e) for e in fn()], "agent.trace_events"
    attr = getattr(agent, "trace", None)
    if isinstance(attr, list):
        return [_event_dict(e) for e in attr], "agent.trace"
    if db_path != ":memory:" and os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trace'").fetchone():
                rows = conn.execute("SELECT * FROM trace ORDER BY seq").fetchall()
                return [_event_dict(dict(r)) for r in rows], "sqlite:trace"
        finally:
            conn.close()
    return [], "none"


def _as_result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    return dict(result or {})


def _evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Each released record with its speaker. The speaker is what lets the
    answerer attribute ownership; dropping it once cost half of U."""
    rows = []
    for item in result.get("evidence") or []:
        if hasattr(item, "__dataclass_fields__"):
            item = asdict(item)
        if not isinstance(item, dict):
            continue
        row = {"record_id": str(item.get("record_id") or ""), "text": str(item.get("text") or "")}
        for key in ("principal_id", "speaker", "author_id", "role"):
            if item.get(key):
                row[key] = str(item[key])
        rows.append(row)
    return rows


def run_episode(job: dict[str, Any]) -> dict[str, Any]:
    """The child entry point. Returns plain data (picklable, JSON-able)."""
    t0 = time.monotonic()
    out: dict[str, Any] = {"episode_id": job["episode"]["episode_id"], "queries": [], "traces": [],
                           "error": None, "timing": {}, "unplaced": [], "memory_llm": {}}
    try:
        _install_wall(job["forbidden_paths"])
        os.environ.update(job["env"])
        if job.get("fake_llm"):
            out["memory_fake"] = _install_memory_fake()
        cls = _import_agent(job["agent_path"])
        agent = _construct(cls, job["db_path"], job["prompts_dir"])
        episode = job["episode"]
        eid = episode["episode_id"]
        cps = [r for r in read_jsonl(job["stripped_path"]) if r.get("episode_id") == eid]
        turns = list(episode.get("turns") or [])
        before, after, unplaced = place_checkpoints([t["turn_id"] for t in turns], cps)
        out["unplaced"] = [c["checkpoint_id"] for c in unplaced]
        ingest_s = query_s = 0.0
        last_usage: dict[str, Any] = {}

        def ask(cp: dict[str, Any], placed_after: str) -> None:
            nonlocal query_s, last_usage
            assert_no_hidden_fields(cp, where=f"checkpoint {cp.get('checkpoint_id')}")
            tq = time.monotonic()
            result = _as_result_dict(agent.query(dict(cp)))
            query_s += time.monotonic() - tq
            debug = result.get("debug") or {}
            if isinstance(debug.get("memory_llm"), dict):
                last_usage = debug["memory_llm"]
            out["queries"].append({
                "checkpoint_id": cp["checkpoint_id"], "placed_after": placed_after,
                "action": str(result.get("action") or ""), "answer": str(result.get("answer") or ""),
                "used_record_ids": [str(r) for r in result.get("used_record_ids") or []],
                "evidence": _evidence(result), "debug": debug,
            })

        ti = time.monotonic()
        agent.reset(episode)
        ingest_s += time.monotonic() - ti
        for cp in before:
            ask(cp, "")
        for turn in turns:
            ti = time.monotonic()
            agent.ingest(turn)
            ingest_s += time.monotonic() - ti
            for cp in after.get(turn["turn_id"], []):
                ask(cp, turn["turn_id"])
        for cp in unplaced:
            ask(cp, turns[-1]["turn_id"] if turns else "")
        out["traces"], out["trace_source"] = _drain_trace(agent, job["db_path"])
        if not last_usage:
            try:
                from memory_system import llm as memory_llm

                last_usage = memory_llm.usage()
            except Exception:  # noqa: BLE001 -- accounting must never fail an episode
                last_usage = {}
        out["memory_llm"] = last_usage
        out["timing"] = {"ingest_s": round(ingest_s, 3), "query_s": round(query_s, 3), "n_turns": len(turns)}
        close = getattr(agent, "close", None)
        if callable(close):
            close()
    except BaseException as exc:  # noqa: BLE001 -- report, never kill the pool
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-4000:]
    out["timing"]["wall_s"] = round(time.monotonic() - t0, 3)
    return out


# =====================================================================
# Parent side
# =====================================================================


@dataclass
class EvalResult:
    out_dir: Path
    predictions: list[dict[str, Any]]
    traces: list[dict[str, Any]]
    report: dict[str, Any]
    episode_errors: dict[str, str] = field(default_factory=dict)


def _forbidden_paths(cfg: Config, dataset: Dataset) -> list[str]:
    paths = set()
    root = Path(cfg.data_root)
    if root.is_dir():
        paths.update(str(p) for p in root.glob(f"*/{ANNOTATED_NAME}"))
    for dom in dataset.domains:
        paths.add(str(root / dom / ANNOTATED_NAME))
    return sorted(paths)


def _find_reusable(eid: str, phash: str, reuse_from: list[Path]) -> Path | None:
    for src in reuse_from:
        cand = Path(src) / "episodes" / safe_name(eid)
        meta = read_json(cand / "result.json", {})
        if meta.get("prompts_hash") == phash and not meta.get("error") and (cand / "predictions.jsonl").is_file():
            return cand
    return None


async def evaluate(
    dataset: Dataset, episode_ids: list[str], prompts_dir: Path, out_dir: Path, *,
    client: LLMClientProtocol, cfg: Config | None = None, agent_path: str | None = None,
    reuse_from: list[Path] | None = None, label: str = "",
) -> EvalResult:
    cfg = cfg or config_mod.get()
    agent_path = agent_path or cfg.agent_path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    phash = prompts_hash(prompts_dir, agent_path)
    episodes = dataset.episodes_by_id
    episode_ids = [e for e in episode_ids if e in episodes]

    # THE WALL, parent half: the only checkpoint file the children may read.
    stripped_path = out_dir / STRIPPED_NAME
    stripped = [visible_checkpoint(cp) for eid in episode_ids for cp in dataset.checkpoints_for(eid)]
    for row in stripped:
        assert_no_hidden_fields(row, where=STRIPPED_NAME)
    write_jsonl(stripped_path, stripped)
    visible_by_id = {c["checkpoint_id"]: c for c in stripped}

    env = cfg.memory_llm_env() | {"SHM_PROMPTS_DIR": str(Path(prompts_dir).resolve())}
    if cfg.fake_llm:
        env["SHM_FAKE_LLM"] = "1"
    forbidden = _forbidden_paths(cfg, dataset)

    reused: dict[str, Path] = {}
    jobs: list[dict[str, Any]] = []
    for eid in episode_ids:
        ep_dir = out_dir / "episodes" / safe_name(eid)
        src = _find_reusable(eid, phash, list(reuse_from or []))
        if src is not None and src.resolve() != ep_dir.resolve():
            if ep_dir.exists():
                shutil.rmtree(ep_dir)
            shutil.copytree(src, ep_dir, ignore=shutil.ignore_patterns("*.db", "*.db-*"))
            reused[eid] = src
            continue
        if ep_dir.exists():
            shutil.rmtree(ep_dir)
        ep_dir.mkdir(parents=True)
        jobs.append({
            "episode": strip_hidden_fields(episodes[eid]), "stripped_path": str(stripped_path),
            "db_path": str(ep_dir / "memory.db"), "prompts_dir": str(Path(prompts_dir).resolve()),
            "agent_path": agent_path, "env": env, "fake_llm": cfg.fake_llm, "forbidden_paths": forbidden,
        })

    render_stats: dict[str, Any] = {"n": 0, "n_degraded": 0, "kinds": {}, "usage": {}, "n_empty_filled": 0}
    errors: dict[str, str] = {}
    episode_reports: dict[str, dict[str, Any]] = {}

    async def render_one(eid: str, q: dict[str, Any]) -> dict[str, Any]:
        cp = visible_by_id[q["checkpoint_id"]]
        episode = episodes[eid]
        record = {
            "checkpoint_id": cp["checkpoint_id"], "asker": cp.get("asker") or {},
            "query_text": cp.get("query_text", ""), "evidence": q["evidence"], "action": q["action"],
            "relationships": requester_relationships(episode, cp.get("asker") or {}),
            "domain": episode.get("domain") or "",
        }
        assert_no_hidden_fields(record, where="answerer record")
        output, meta = await render_answer(client, record, cfg)
        render_stats["n"] += 1
        if meta.get("degraded"):
            render_stats["n_degraded"] += 1
            kind = meta.get("error_kind", "no_response")
            render_stats["kinds"][kind] = render_stats["kinds"].get(kind, 0) + 1
        render_stats["n_empty_filled"] += int(bool(meta.get("empty_answer_filled")))
        for k, v in (meta.get("usage") or {}).items():
            render_stats["usage"][k] = render_stats["usage"].get(k, 0) + int(v or 0)
        return asdict(make_prediction_row(
            checkpoint_id=cp["checkpoint_id"], episode_id=eid, as_of_turn_id=cp.get("as_of_turn_id", ""),
            asker=cp.get("asker") or {}, action=output["action"], answer=output["answer"],
            answer_structured=output.get("answer_structured"), used_record_ids=output.get("used_record_ids"),
            debug={"agent_action": q["action"], "agent_answer": q["answer"][:2000], "evidence": q["evidence"],
                   "agent": q["debug"], "placed_after": q["placed_after"]},
            trace_seq=q["trace_seq"], render=meta,
        ))

    def error_row(eid: str, cp: dict[str, Any], why: str) -> dict[str, Any]:
        # Scored like any other prediction (a refusal), and flagged so the
        # gate can refuse a candidate that crashes where the champion did not.
        return asdict(make_prediction_row(
            checkpoint_id=cp["checkpoint_id"], episode_id=eid, as_of_turn_id=cp.get("as_of_turn_id", ""),
            asker=cp.get("asker") or {}, action="refuse", answer="",
            debug={"episode_error": why[:300]}, render={"skipped": "episode error"},
        ))

    async def finish_episode(res: dict[str, Any]) -> None:
        eid = res["episode_id"]
        ep_dir = out_dir / "episodes" / safe_name(eid)
        by_cid: dict[str, list[int]] = {}
        for ev in res.get("traces") or []:
            if ev.get("checkpoint_id"):
                by_cid.setdefault(ev["checkpoint_id"], []).append(int(ev.get("seq") or 0))
        for q in res["queries"]:
            q["trace_seq"] = sorted(by_cid.get(q["checkpoint_id"], []))
        preds = list(await asyncio.gather(*(render_one(eid, q) for q in res["queries"])))
        answered = {p["checkpoint_id"] for p in preds}
        if res.get("error"):
            errors[eid] = res["error"]
            log.warning("episode %s failed: %s", eid, res["error"])
            preds += [error_row(eid, cp, res["error"]) for cp in stripped
                      if cp["episode_id"] == eid and cp["checkpoint_id"] not in answered]
        write_jsonl(ep_dir / "predictions.jsonl", preds)
        write_jsonl(ep_dir / "traces.jsonl", [dict(ev, episode_id=eid) for ev in res.get("traces") or []])
        meta = {"episode_id": eid, "prompts_hash": phash, "agent_path": agent_path, "error": res.get("error"),
                "traceback": res.get("traceback"), "timing": res.get("timing"), "unplaced": res.get("unplaced"),
                "memory_llm": res.get("memory_llm"), "trace_source": res.get("trace_source"),
                "memory_fake": res.get("memory_fake"), "n_queries": len(res["queries"])}
        write_json(ep_dir / "result.json", meta)
        episode_reports[eid] = meta

    if jobs:
        ctx = multiprocessing.get_context("spawn")
        loop = asyncio.get_running_loop()
        workers = max(1, min(cfg.episode_workers, len(jobs)))
        # At most `workers` episodes are handed to the pool at a time, so the
        # timeout below measures an episode's RUNNING time. Submitting every
        # job at once made it measure queueing time too: on the first full
        # office run (17 episodes, 3 workers) the 13 episodes behind the
        # first batch "timed out" while waiting their turn, their children
        # ran to completion anyway, and the parent discarded their answers.
        slots = asyncio.Semaphore(workers)
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            async def run(job: dict[str, Any]) -> None:
                eid = job["episode"]["episode_id"]
                async with slots:
                    await _run_one(job, eid)

            async def _run_one(job: dict[str, Any], eid: str) -> None:
                try:
                    fut = loop.run_in_executor(pool, run_episode, job)
                    try:
                        res = await asyncio.wait_for(fut, timeout=cfg.episode_timeout_s)
                    except asyncio.TimeoutError:
                        # Best effort only: a ProcessPoolExecutor future that
                        # has started running cannot actually be interrupted,
                        # so the worker keeps the episode's process until it
                        # finishes on its own; this just stops WAITING on it
                        # and lets the run move on and report the timeout.
                        fut.cancel()
                        res = {
                            "episode_id": eid, "queries": [], "traces": [],
                            "error": f"timeout: episode exceeded EPISODE_TIMEOUT_S={cfg.episode_timeout_s:g}s",
                            "timing": {},
                        }
                except (BrokenProcessPool, OSError, RuntimeError) as exc:
                    res = {"episode_id": eid, "queries": [], "traces": [], "error": f"{type(exc).__name__}: {exc}",
                           "timing": {}}
                await finish_episode(res)

            await asyncio.gather(*(run(j) for j in jobs))

    for eid, src in reused.items():
        meta = read_json(out_dir / "episodes" / safe_name(eid) / "result.json", {})
        meta["reused_from"] = str(src)
        episode_reports[eid] = meta

    # Merge, in dataset order, so files diff cleanly between iterations.
    order = {cp["checkpoint_id"]: i for i, cp in enumerate(stripped)}
    predictions: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for eid in episode_ids:
        ep_dir = out_dir / "episodes" / safe_name(eid)
        predictions += read_jsonl(ep_dir / "predictions.jsonl")
        traces += read_jsonl(ep_dir / "traces.jsonl")
    predictions.sort(key=lambda p: order.get(p["checkpoint_id"], 1 << 30))
    write_jsonl(out_dir / "predictions.jsonl", predictions)
    write_jsonl(out_dir / "traces.jsonl", traces)

    memory_total: dict[str, int] = {}
    for meta in episode_reports.values():
        for k, v in (meta.get("memory_llm") or {}).items():
            if isinstance(v, (int, float)):
                memory_total[k] = memory_total.get(k, 0) + int(v)
    report = {
        "label": label, "prompts_hash": phash, "agent_path": agent_path,
        "n_episodes": len(episode_ids), "n_checkpoints": len(stripped), "n_predictions": len(predictions),
        "n_reused_episodes": len(reused), "n_run_episodes": len(jobs), "wall_s": round(time.monotonic() - t0, 3),
        "episode_errors": errors | {e: m["error"] for e, m in episode_reports.items() if m.get("error")},
        "n_unplaced": sum(len(m.get("unplaced") or []) for m in episode_reports.values()),
        "render": render_stats, "memory_llm_total": memory_total,
        "episodes": {e: {k: m.get(k) for k in ("timing", "memory_llm", "unplaced", "error", "reused_from",
                                               "trace_source", "memory_fake", "n_queries")}
                     for e, m in sorted(episode_reports.items())},
    }
    report["render"]["degraded_rate"] = round(render_stats["n_degraded"] / render_stats["n"], 4) \
        if render_stats["n"] else 0.0
    write_json(out_dir / "eval_report.json", report)
    return EvalResult(out_dir=out_dir, predictions=predictions, traces=traces, report=report,
                      episode_errors=dict(report["episode_errors"]))


def load_eval(out_dir: Path) -> EvalResult:
    out_dir = Path(out_dir)
    report = read_json(out_dir / "eval_report.json", {}) or {}
    return EvalResult(out_dir=out_dir, predictions=read_jsonl(out_dir / "predictions.jsonl"),
                      traces=read_jsonl(out_dir / "traces.jsonl"), report=report,
                      episode_errors=dict(report.get("episode_errors") or {}))
