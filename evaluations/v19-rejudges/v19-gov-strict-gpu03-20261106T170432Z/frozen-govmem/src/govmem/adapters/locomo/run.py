"""Run govmem and the full-context baseline on LoCoMo (locomo10), same answerer and judge for both.

    python -m govmem.adapters.locomo.run --run-dir runs/locomo_v1 --workers 10

Per question it records the released evidence, and evidence-turn recall: whether the gold evidence turns
(dia_ids) are among the sources of the released records, and whether any live record from them exists at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from govmem.adapters.gatemem.data import read_jsonl, write_jsonl
from govmem.adapters.locomo import answer as A
from govmem.adapters.locomo.data import QUESTIONER, full_transcript, load, to_conversation
from govmem.api import Memory
from govmem.config import Settings
from govmem.interpret.interpreter import Turn
from govmem.llm import ChatClient, ReplyCache

log = logging.getLogger("govmem.locomo")
DATA = Path(__file__).resolve().parents[4] / "data" / "locomo" / "locomo10.json"


def _logging(path: Path | None = None) -> None:
    hs: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if path is not None:
        hs.append(logging.FileHandler(path))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(processName)s %(message)s", handlers=hs, force=True)
    for n in ("httpx", "httpcore", "sentence_transformers"):
        logging.getLogger(n).setLevel(logging.WARNING)


def run_conversation(index: int, data_path: str, run_dir: str, no_model: bool) -> dict[str, Any]:
    run = Path(run_dir)
    sample = load(Path(data_path))[index]
    conv, _gold = to_conversation(sample)
    cdir = run / "conversations" / conv.conv_id
    cdir.mkdir(parents=True, exist_ok=True)
    _logging(cdir / "conversation.log")
    done = cdir / "done.json"
    if done.exists():
        return json.loads(done.read_text())
    settings = Settings.from_env()
    settings.features.open_access = True
    db = cdir / "memory.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    mem = Memory(str(db), settings, chat=None if no_model else "auto", cache_path=run / "cache" / f"{conv.conv_id}.sqlite")
    mem.declare(conv.principals)
    t0 = time.time()
    for i, t in enumerate(conv.turns, 1):
        mem.ingest(Turn(t["turn_id"], t["timestamp"], t["speaker_id"], t["text"]))
        if i % 50 == 0 or i == len(conv.turns):
            log.info("%s turn %d/%d records=%d calls=%d tokens=%d %.0fs", conv.conv_id, i, len(conv.turns),
                     len(mem.store.records(current_only=False, alive_only=False)), mem.model.calls, mem.model.tokens,
                     time.time() - t0)
    last = conv.turns[-1]["turn_id"]
    rows = []
    for q in conv.questions:
        ev = mem.query(QUESTIONER, q["question"], last)
        rows.append({"qid": q["qid"], "question": q["question"], "action": ev.action, "counters": ev.counters,
                     "evidence": [{k: it[k] for k in ("record_id", "principal_id", "text", "timestamp", "source_turn")}
                                  for it in ev.items]})
    write_jsonl(cdir / "evidence.jsonl", rows)
    live_turns = {r.source_turn for r in mem.store.records(current_only=True)}
    summary = {"conv_id": conv.conv_id, "seconds": round(time.time() - t0, 1), "n_turns": len(conv.turns),
               "n_questions": len(rows), "health": mem.health_report(), "live_source_turns": sorted(live_turns),
               "records": {k: v for k, v in Counter(
                   "tombstoned" if r.tombstoned_at else "superseded" if r.superseded_by else "current"
                   for r in mem.store.records(current_only=False, alive_only=False)).items()},
               "reorganisations": [dict(r) for r in mem.store.q(
                   "SELECT op, status, COUNT(*) n FROM _reorgs GROUP BY op, status")]}
    (cdir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    mem.close()
    done.write_text(json.dumps({k: summary[k] for k in ("conv_id", "seconds", "n_turns", "n_questions", "records")}))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--conversations", type=int, default=0, help="first N conversations (0 = all)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--answer-workers", type=int, default=8)
    ap.add_argument("--systems", default="govmem,full_context")
    ap.add_argument("--no-model", action="store_true")
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    _logging(run / "run.log")
    samples = load(Path(args.data))
    idx = list(range(len(samples)))[: args.conversations or None]
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    settings = Settings.from_env()
    (run / "run.json").write_text(json.dumps({"benchmark": "locomo10", "conversations": [samples[i]["sample_id"] for i in idx],
                                              "systems": systems, "memory_model": settings.memory.model,
                                              "answer_model": settings.answer.model, "judge_model": settings.judge.model,
                                              "features": settings.features.to_dict() | {"open_access": True}}, indent=1))
    convs, gold, names = {}, {}, {}
    for i in idx:
        c, g = to_conversation(samples[i])
        convs[c.conv_id] = c
        gold.update(g)
        names[c.conv_id] = {p["id"]: p["display_name"] for p in c.principals}

    if "govmem" in systems:
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=mp.get_context("spawn")) as ex:
            futs = {ex.submit(run_conversation, i, args.data, str(run), args.no_model): i for i in idx}
            for f in as_completed(futs):
                try:
                    s = f.result()
                    log.info("conversation done %s in %ss, %d questions, records %s", s["conv_id"], s["seconds"],
                             s["n_questions"], s["records"])
                except Exception as exc:  # noqa: BLE001
                    log.exception("conversation failed %s: %s", samples[futs[f]]["sample_id"], exc)
        log.info("memory phase %.0fs", time.time() - t0)

    chat = ChatClient(settings.answer)
    jchat = ChatClient(settings.judge)
    acache = ReplyCache(run / "cache" / "answers.sqlite")
    jcache = ReplyCache(run / "cache" / "judge.sqlite")
    jobs = []
    for cid, conv in convs.items():
        if "govmem" in systems and (run / "conversations" / cid / "evidence.jsonl").exists():
            for row in read_jsonl(run / "conversations" / cid / "evidence.jsonl"):
                jobs.append(("govmem", cid, row["qid"], row["question"], row["evidence"], row))
        if "full_context" in systems:
            items = full_transcript(conv)
            for q in conv.questions:
                jobs.append(("full_context", cid, q["qid"], q["question"], items, None))

    def one(job: tuple) -> dict[str, Any]:
        system, cid, qid, question, items, row = job
        pred, meta = A.answer(chat, acache, question, items, names[cid])
        g = gold[qid]
        ok, jfb = A.judge(jchat, jcache, question, g["answer"], pred)
        out = {"system": system, "conv_id": cid, "qid": qid, "category": g["category"],
               "category_name": g["category_name"], "question": question, "gold": g["answer"], "pred": pred,
               "f1": A.f1(pred, g["answer"], g["category"]), "j": ok, "judge_fallback": jfb,
               "answer_degraded": meta["degraded"], "prompt_chars": meta["prompt_chars"]}
        if row is not None:
            src = {e["source_turn"] for e in row["evidence"]}
            ev = g["evidence"]
            out["n_released"] = len(row["evidence"])
            out["mode"] = row["counters"].get("mode")
            out["evidence_recall_released"] = (sum(1 for d in ev if d in src) / len(ev)) if ev else None
        return out

    results = []
    with ThreadPoolExecutor(max_workers=args.answer_workers) as ex:
        futs = [ex.submit(one, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            results.append(f.result())
            if i % 200 == 0 or i == len(futs):
                log.info("answered+judged %d/%d", i, len(futs))
    results.sort(key=lambda r: (r["system"], r["qid"]))
    # stored-evidence recall needs each conversation's live source turns
    live = {}
    for cid in convs:
        p = run / "conversations" / cid / "summary.json"
        if p.exists():
            live[cid] = set(json.loads(p.read_text())["live_source_turns"])
    for r in results:
        if r["system"] == "govmem" and r["conv_id"] in live and gold[r["qid"]]["evidence"]:
            ev = gold[r["qid"]]["evidence"]
            r["evidence_recall_stored"] = sum(1 for d in ev if d in live[r["conv_id"]]) / len(ev)
    write_jsonl(run / "results.jsonl", results)

    scores: dict[str, Any] = {}
    for system in systems:
        rs = [r for r in results if r["system"] == system]
        if not rs:
            continue
        by = defaultdict(list)
        for r in rs:
            by[r["category_name"]].append(r)
        def agg(xs: list[dict]) -> dict[str, Any]:
            js = [x["j"] for x in xs if x["j"] is not None]
            d = {"n": len(xs), "F1": round(sum(x["f1"] for x in xs) / len(xs), 4),
                 "J": round(sum(js) / len(js), 4) if js else None}
            rel = [x["evidence_recall_released"] for x in xs if x.get("evidence_recall_released") is not None]
            sto = [x["evidence_recall_stored"] for x in xs if x.get("evidence_recall_stored") is not None]
            if rel:
                d["evidence_recall_released"] = round(sum(rel) / len(rel), 4)
            if sto:
                d["evidence_recall_stored"] = round(sum(sto) / len(sto), 4)
            return d
        scores[system] = {"overall": agg(rs), **{k: agg(v) for k, v in sorted(by.items())},
                          "judge_fallbacks": sum(1 for r in rs if r["judge_fallback"]),
                          "answer_degraded": sum(1 for r in rs if r["answer_degraded"]),
                          "modes": dict(Counter(r.get("mode") for r in rs if r.get("mode"))),
                          "mean_prompt_chars": int(sum(r["prompt_chars"] for r in rs) / len(rs))}
        log.info("SCORES %s %s", system, json.dumps(scores[system]["overall"]))
    (run / "scores.json").write_text(json.dumps(scores, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
