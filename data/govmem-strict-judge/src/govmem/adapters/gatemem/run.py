"""Run govmem on GateMem: ingest + query per episode (parallel processes), answer, release check, judge, score.

    python -m govmem.adapters.gatemem.run --domain medical --episodes 2 --run-dir runs/smoke_medical

Resumable: a finished episode (episodes/<id>/done.json) is not re-run; model replies are cached on disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from govmem.adapters.gatemem import answerer
from govmem.adapters.gatemem.agent import GateMemAgent
from govmem.adapters.gatemem.data import GateMemData, annotation, read_jsonl, requester_relationships, \
    visible_checkpoint, write_jsonl
from govmem.config import Settings
from govmem.core.store import Store
from govmem.eval.diagnose import diagnose
from govmem.eval.judge import aggregate, judge_all
from govmem.llm import ChatClient, ReplyCache
from govmem.release.check import check_release

log = logging.getLogger("govmem.run")

HEALTH_THRESHOLDS = {"parse_fail_rate": 0.01, "fallback_rate": 0.02, "budget_refused": 0,
                     "answer_degraded_rate": 0.005, "judge_fallbacks": 0, "episode_errors": 0}


def _setup_logging(path: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if path is not None:
        handlers.append(logging.FileHandler(path))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(processName)s %(message)s", handlers=handlers,
                        force=True)
    for noisy in ("httpx", "httpcore", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ===================================================================== phase 1: memory, per episode process
def run_episode(episode_id: str, domain: str, data_root: str, run_dir: str, no_model: bool,
                continuous: bool = False) -> dict[str, Any]:
    run = Path(run_dir)
    ep_dir = run / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(ep_dir / "episode.log")
    done = ep_dir / "done.json"
    if done.exists():
        return json.loads(done.read_text())
    data = GateMemData(Path(data_root), domain, continuous=continuous)
    episode = next(e for e in data.episodes if e["episode_id"] == episode_id)
    cps = [visible_checkpoint(c) for c in data.checkpoints_for(episode_id)]
    by_turn: dict[str, list[dict[str, Any]]] = {}
    for c in cps:
        by_turn.setdefault(c["as_of_turn_id"], []).append(c)
    settings = Settings.from_env()
    agent = GateMemAgent(str(ep_dir / "memory.db"), settings, chat=None if no_model else "auto",
                         cache_path=run / "cache" / f"{episode_id}.sqlite")
    t0 = time.time()
    agent.reset(episode)
    rows: list[dict[str, Any]] = []
    turns = episode["turns"]
    for i, turn in enumerate(turns, 1):
        agent.ingest(turn)
        for c in by_turn.get(turn["turn_id"], []):
            q = agent.query(c)
            rows.append({"checkpoint": c, "result": q})
        if i % (100 if continuous else 25) == 0 or i == len(turns):
            m = agent.mem
            assert m is not None
            log.info("%s turn %d/%d records=%d calls=%d tokens=%d %.0fs", episode_id[:40], i, len(turns),
                     len(m.store.records(current_only=False, alive_only=False)), m.model.calls, m.model.tokens,
                     time.time() - t0)
    assert agent.mem is not None
    write_jsonl(ep_dir / "evidence.jsonl", rows)
    mem = agent.mem
    ledger = [dict(r) for r in mem.store.q("SELECT op, status, COUNT(*) n FROM _reorgs GROUP BY op, status")]
    summary = {
        "episode_id": episode_id, "seconds": round(time.time() - t0, 1), "n_turns": len(turns),
        "n_checkpoints": len(rows), "health": mem.health_report(),
        "records": {r["k"]: r["n"] for r in mem.store.q(
            "SELECT CASE WHEN tombstoned_at IS NOT NULL THEN 'tombstoned' WHEN superseded_by IS NOT NULL "
            "THEN 'superseded' ELSE 'current' END k, COUNT(*) n FROM records GROUP BY k")},
        "reorganisations": ledger,
        "ops": [dict(r) for r in mem.store.q("SELECT op, outcome, reason_code, COUNT(*) n FROM _op_log "
                                             "GROUP BY op, outcome, reason_code ORDER BY n DESC")],
        "features": settings.features.to_dict(),
    }
    (ep_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    mem.close()
    done.write_text(json.dumps(summary, default=str))
    return summary


# ===================================================================== phase 2: answer + release check
def answer_all(run: Path, data: GateMemData, episode_ids: list[str], settings: Settings, workers: int,
               salt: str = "") -> list[dict]:
    chat = ChatClient(settings.answer)
    cache = ReplyCache(run / "cache" / "answers.sqlite")
    episodes = {e["episode_id"]: e for e in data.episodes}
    jobs = []
    for eid in episode_ids:
        for row in read_jsonl(run / "episodes" / eid / "evidence.jsonl"):
            jobs.append((eid, row))

    def one(job: tuple[str, dict]) -> dict[str, Any]:
        eid, row = job
        c, q = row["checkpoint"], row["result"]
        ep = episodes[eid]
        out, meta = answerer.answer(chat, cache, settings.gatemem_repo, ep.get("domain") or data.domain, c["asker"],
                                    c.get("query_text") or "", q["evidence"],
                                    requester_relationships(ep, c["asker"]), q["action"], salt=salt)
        reason = ""
        if settings.features.release_check:
            store = Store(str(run / "episodes" / eid / "memory.db"))
            try:
                out, reason = check_release(store, out, [e["record_id"] for e in q["evidence"]],
                                            [e["text"] for e in q["evidence"]])
            finally:
                store.conn.close()
        block = answerer.format_memory_block(q["evidence"])
        out["memory_audit"] = {"prompt_context": {"text": block}}
        out["debug"] = {"agent_action": q["action"], "retrieval": q["debug"]["retrieval"], "release_reason": reason,
                        "render": meta, "evidence_ids": [e["record_id"] for e in q["evidence"]]}
        return {"checkpoint_id": c["checkpoint_id"], "episode_id": eid, "as_of_turn_id": c["as_of_turn_id"],
                "asker": c["asker"], "query_text": c.get("query_text"), "output": out}

    preds: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            preds.append(f.result())
            if i % 10 == 0 or i == len(futs):
                log.info("answered %d/%d", i, len(futs))
    preds.sort(key=lambda p: p["checkpoint_id"])
    return preds


# ===================================================================== health
def health_summary(run: Path, episode_ids: list[str], preds: list[dict], verdicts: list[dict]) -> dict[str, Any]:
    counters: dict[str, int] = {}
    store_c: dict[str, int] = {}
    errors = 0
    for eid in episode_ids:
        p = run / "episodes" / eid / "summary.json"
        if not p.exists():
            errors += 1
            continue
        s = json.loads(p.read_text())
        for k, v in s["health"]["model"]["counters"].items():
            counters[k] = counters.get(k, 0) + v
        for k, v in s["health"]["store"].items():
            store_c[k] = store_c.get(k, 0) + v
    call_types = sorted({k.split(".")[0] for k in counters})
    per_type = {}
    flags = []
    for ct in call_types:
        req = counters.get(f"{ct}.requested", 0)
        pf = counters.get(f"{ct}.parse_fail", 0)
        br = counters.get(f"{ct}.budget_refused", 0)
        per_type[ct] = {"requested": req, "calls": counters.get(f"{ct}.calls", 0), "ok": counters.get(f"{ct}.ok", 0),
                        "cache_hit": counters.get(f"{ct}.cache_hit", 0), "retried": counters.get(f"{ct}.retried", 0),
                        "parse_fail": pf, "budget_refused": br,
                        "transport_error": counters.get(f"{ct}.transport_error", 0),
                        "parse_fail_rate": round(pf / req, 4) if req else 0.0}
        if req and pf / req > HEALTH_THRESHOLDS["parse_fail_rate"]:
            flags.append(f"{ct}: parse_fail_rate {pf / req:.3f} > {HEALTH_THRESHOLDS['parse_fail_rate']}")
        if br > HEALTH_THRESHOLDS["budget_refused"]:
            flags.append(f"{ct}: budget_refused {br}")
    n_turns = sum(json.loads((run / "episodes" / e / "summary.json").read_text())["n_turns"]
                  for e in episode_ids if (run / "episodes" / e / "summary.json").exists())
    fb = store_c.get("label.fallback", 0)
    if n_turns and fb / n_turns > HEALTH_THRESHOLDS["fallback_rate"]:
        flags.append(f"label: fallback_rate {fb / n_turns:.3f} > {HEALTH_THRESHOLDS['fallback_rate']}")
    degraded = sum(1 for p in preds if p["output"]["debug"]["render"].get("degraded"))
    if preds and degraded / len(preds) > HEALTH_THRESHOLDS["answer_degraded_rate"]:
        flags.append(f"answer: degraded {degraded}/{len(preds)}")
    jf = sum(1 for v in verdicts if v.get("judge_fallback"))
    if jf > HEALTH_THRESHOLDS["judge_fallbacks"]:
        flags.append(f"judge: {jf} fallbacks to the rule scorer")
    if errors:
        flags.append(f"episodes: {errors} without a summary")
    return {"model_calls": per_type, "store_counters": store_c, "answer_degraded": degraded, "judge_fallbacks": jf,
            "release_changes": sum(1 for p in preds if p["output"]["debug"].get("release_reason")),
            "flags": flags, "eligible": not flags}


# ===================================================================== main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="medical")
    ap.add_argument("--episodes", type=int, default=2)
    ap.add_argument("--episode-ids", nargs="*")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--workers", type=int, default=2, help="episode processes")
    ap.add_argument("--answer-workers", type=int, default=6)
    ap.add_argument("--no-model", action="store_true", help="memory model disabled: deterministic fallbacks")
    ap.add_argument("--skip-answer", action="store_true")
    ap.add_argument("--rule-judge", action="store_true", help="no LLM judge")
    ap.add_argument("--seed-cache-from", help="copy model-reply caches from an earlier run directory")
    ap.add_argument("--reanswer", type=int, default=0, help="re-answer every checkpoint K more times (noise floor)")
    ap.add_argument("--continuous", action="store_true", help="one memory per domain, never reset (episodes in order)")
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    _setup_logging(run / "run.log")
    if args.seed_cache_from:
        import shutil
        (run / "cache").mkdir(exist_ok=True)
        for f in (Path(args.seed_cache_from) / "cache").glob("*.sqlite"):
            if not (run / "cache" / f.name).exists():
                shutil.copy(f, run / "cache" / f.name)
    settings = Settings.from_env()
    data_root = settings.gatemem_repo / "bench" / "data"
    data = GateMemData(data_root, args.domain, continuous=args.continuous)
    eps = data.select(args.episodes or None, args.episode_ids)
    ids = [e["episode_id"] for e in eps]
    manifest = {"domain": args.domain, "episodes": ids, "no_model": args.no_model, "continuous": args.continuous,
                "memory_model": settings.memory.model, "answer_model": settings.answer.model,
                "judge_model": settings.judge.model, "features": settings.features.to_dict(),
                "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (run / "run.json").write_text(json.dumps(manifest, indent=1))
    log.info("run %s: %d episodes %s", run, len(ids), ids)

    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=max(1, args.workers), mp_context=ctx) as ex:
        futs = {ex.submit(run_episode, eid, args.domain, str(data_root), str(run), args.no_model, args.continuous): eid
                for eid in ids}
        for f in as_completed(futs):
            eid = futs[f]
            try:
                s = f.result()
                log.info("episode done %s in %ss, %d checkpoints, records %s", eid, s["seconds"], s["n_checkpoints"],
                         s["records"])
            except Exception as exc:  # noqa: BLE001
                log.exception("episode failed %s: %s", eid, exc)
    log.info("memory phase %.0fs", time.time() - t0)
    if args.skip_answer:
        return 0

    preds = answer_all(run, data, [i for i in ids if (run / "episodes" / i / "done.json").exists()], settings,
                       args.answer_workers)
    write_jsonl(run / "predictions.jsonl", preds)
    anns = {c["checkpoint_id"]: annotation(c) for c in data.checkpoints}
    jchat = None if args.rule_judge else ChatClient(settings.judge)
    verdicts = judge_all(preds, anns, chat=jchat, cache=ReplyCache(run / "cache" / "judge.sqlite"),
                         repo=settings.gatemem_repo)
    write_jsonl(run / "verdicts.jsonl", verdicts)
    scores = aggregate(verdicts)
    health = health_summary(run, ids, preds, verdicts)
    (run / "scores.json").write_text(json.dumps(scores, indent=1))
    (run / "health.json").write_text(json.dumps(health, indent=1))
    log.info("SCORES U=%.3f A=%.3f F=%.3f OR=%.3f MGS=%.3f | strict A=%.3f F=%.3f MGS=%.3f | n=%d",
             scores["U"], scores["A"], scores["F"], scores["OR"], scores["MGS"], scores["A_strict"],
             scores["F_strict"], scores["MGS_strict"], scores["n"])
    log.info("HEALTH eligible=%s flags=%s", health["eligible"], health["flags"])
    cps = {c["checkpoint_id"]: c for c in data.checkpoints}
    diag = diagnose(run, ids, verdicts, cps)
    log.info("DIAGNOSIS utility %s | missed patterns %s | deletion exposures %s",
             diag["utility_failures_by_stage"], diag["missed_patterns_by_stage"], diag["deletion_exposures_by_timing"])
    if args.reanswer:
        base = {v["checkpoint_id"]: v for v in verdicts}
        rows = [{"repeat": 0, **{k: scores[k] for k in ("U", "A", "F", "MGS", "MGS_strict")}}]
        for k in range(1, args.reanswer + 1):
            pk = answer_all(run, data, [i for i in ids if (run / "episodes" / i / "done.json").exists()], settings,
                            args.answer_workers, salt=f":repeat{k}")
            write_jsonl(run / f"predictions_repeat{k}.jsonl", pk)
            vk = judge_all(pk, anns, chat=jchat, cache=ReplyCache(run / "cache" / "judge.sqlite"),
                           repo=settings.gatemem_repo)
            write_jsonl(run / f"verdicts_repeat{k}.jsonl", vk)
            sk = aggregate(vk)
            flips = sum(1 for v in vk if (v["utility_correct"], v["privacy_leak"], v["deletion_leak"]) !=
                        (base[v["checkpoint_id"]]["utility_correct"], base[v["checkpoint_id"]]["privacy_leak"],
                         base[v["checkpoint_id"]]["deletion_leak"]))
            rows.append({"repeat": k, "flips_vs_first": flips,
                         **{m: sk[m] for m in ("U", "A", "F", "MGS", "MGS_strict")}})
            log.info("REANSWER %d: U=%.3f A=%.3f F=%.3f MGS=%.3f, %d checkpoint verdicts flipped",
                     k, sk["U"], sk["A"], sk["F"], sk["MGS"], flips)
        (run / "noise.json").write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    raise SystemExit(main())
