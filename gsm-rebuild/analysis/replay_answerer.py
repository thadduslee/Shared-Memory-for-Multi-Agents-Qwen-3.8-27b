"""Replay only the baselines' answerer on GSM checkpoints, with controlled evidence variants.

Analysis only (reads hidden labels to pick checkpoints and score coverage). The
answerer is the benchmark's own ``BaseMemoryAgent._run_llm`` through its own
``LLMRouter`` (same prompt, model, temperature, token limit, thinking at the
server default). The router is wrapped only to keep the model's reasoning, which
the bench discards, so we can read why it refused.

Variants per checkpoint (the prompt, requester and question never change; only
the MEMORY PROVIDED block does):

  gsm        the exact evidence GSM gave in the judged run
  oracle     only the GSM evidence lines that contain a needed fact (judge_spec.include)
  lc         the long_context baseline's own memory block for the same checkpoint
  none       no memory at all (what the prompt alone makes the answerer do)
  no_wording the judged run's evidence minus records that set approved public wording
             (nicknames / "broad summaries should say ..."); diagnostic heuristic only
  gsm_own    the exact GSM evidence plus the asker's own messages up to the checkpoint (raw text), skipping
             any message GSM deleted or partly deleted; tests whether the asker's own record of their work
             is the context the answerer needs
  gsm_own_masked  as gsm_own, but every value in the asker's messages (numbers, dates, times, codes) masked:
             their record of what they did, without the figures that may since have been deleted
  perm_turns the raw messages GSM permits the asker to see, whole and in order: a message is included when
             at least one of its current records is readable by the asker, all of its current records are,
             and none of it was deleted or masked (GSM's access decisions, long_context's representation)
  all        everything GSM permitted the asker to see at that checkpoint (perfect recall
             under GSM's governance), in turn order, with speaker attribution for full views

Usage: replay_answerer.py RUN DOMAIN OUT.jsonl [--variants gsm,oracle,lc] [--repeats N] [--workers N]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gsm-rebuild/src"))

from bench.eval.metrics import pattern_hits  # noqa: E402

from gsm.eval.gatemem.baseline_answerer import (  # noqa: E402
    BaselineAnswererConfig,
    episode_for_bench,
    load_bench,
    make_router,
)
from gsm.eval.gatemem.sanitize import load_for_method  # noqa: E402

OUT = ROOT / "gsm-rebuild/outputs/gatemem"
ANSWERING = {"answer", "answer_redacted"}
_WORDING = re.compile(
    r"wording|referred to as|refer to it as|call it|broad (?:summar|update|status|customer)|safe (?:label|phrase)"
    r"|outside the (?:core|inner)|sponsor-safe|customer-safe",
    re.IGNORECASE,
)
_LINE = re.compile(r"^Memory \d+ \(speaker=([^)]*)\): (.*)$")


def jl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


class CapturingRouter:
    """Delegates to the bench router; remembers each call's reasoning per thread."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.local = threading.local()

    def complete_result(self, **kw: Any) -> Any:
        res = self.inner.complete_result(**kw)
        msg = ((res.raw or {}).get("choices") or [{}])[0].get("message") or {}
        self.local.reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def parse_block(block: str) -> list[dict[str, Any]]:
    items = []
    for i, line in enumerate(block.splitlines(), 1):
        m = _LINE.match(line.strip())
        if m:
            items.append({"memory_id": f"m{i}", "principal_id": m.group(1), "text": m.group(2)})
    return items


def permitted_items(run: str, domain: str) -> dict[str, list[dict[str, Any]]]:
    """Per checkpoint: every memory GSM allowed the asker (from the run's diagnostics), in turn order."""
    import psycopg

    diag = {d["checkpoint_id"]: d for d in jl(OUT / f"{run}_{domain}/checkpoint_diag.jsonl")}
    speaker: dict[str, str] = {}
    for e in jl(OUT / f"{run}_{domain}/ingest_log.jsonl"):
        for f in e.get("facts", []):
            if f.get("memory_id"):
                speaker[f["memory_id"]] = e["speaker"]
        if e.get("memory_id"):
            speaker[e["memory_id"]] = e["speaker"]
    ids = {a["memory_id"] for d in diag.values() for a in d.get("allowed", [])}
    with psycopg.connect("postgresql://gsm_owner@localhost:55432/gsm") as conn:
        rows = conn.execute(
            "SELECT memory_id, view, text FROM memory_projections WHERE memory_id = ANY(%s)", (list(ids),)
        ).fetchall()
    text = {(m, v): t for m, v, t in rows}
    out: dict[str, list[dict[str, Any]]] = {}
    for cid, d in diag.items():
        items = []
        for a in d.get("allowed", []):
            if a["view"] == "existence_only":
                continue
            t = text.get((a["memory_id"], a["view"]))
            if t is None:
                continue  # scrubbed later (deleted after this checkpoint)
            turn = a.get("turn_id") or ""
            items.append((int(re.sub(r"\D", "", turn) or 0), a["memory_id"], a["view"], t))
        items.sort()
        out[cid] = [
            {"memory_id": m, "text": t, "principal_id": speaker.get(m) if v == "full" else None}
            for _, m, v, t in items
        ]
    return out


def _mask_values(text: str) -> str:
    from gsm.eval.gatemem.adapter import _values

    for v in sorted(_values(text), key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", "[value]", text)
    return text


def permitted_turns(run: str, domain: str, eps: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per checkpoint: the whole messages whose stored content the asker may read (see perm_turns)."""
    out: dict[str, list[dict[str, Any]]] = {}
    ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{domain}/checkpoints.jsonl")}
    allowed = {d["checkpoint_id"]: {a["memory_id"] for a in d.get("allowed", [])}
               for d in jl(OUT / f"{run}_{domain}/checkpoint_diag.jsonl")}
    for snap in jl(OUT / f"{run}_{domain}/memory_snapshots.jsonl"):
        cid = snap["checkpoint_id"]
        c = ck[cid]
        by_turn: dict[str, list[dict[str, Any]]] = {}
        for r in snap["records"]:
            if r["turn_id"]:
                by_turn.setdefault(r["turn_id"], []).append(r)
        items = []
        for t in eps[c["episode_id"]].turns:
            rs = by_turn.get(t.turn_id, [])
            cur = [r for r in rs if r["state"] == "current"]
            wiped = any(r["state"] == "deleted" or "[removed]" in (r["text"] or "") for r in rs)
            if cur and not wiped and all(r["memory_id"] in allowed[cid] for r in cur):
                items.append({"memory_id": f"turn-{t.turn_id}", "text": t.text, "principal_id": t.speaker_principal_id})
            if t.turn_id == c["as_of_turn_id"]:
                break
        out[cid] = items
    return out


def own_messages(run: str, domain: str, eps: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per checkpoint: the asker's own messages up to it, minus any whose memory GSM deleted or masked."""
    out: dict[str, list[dict[str, Any]]] = {}
    ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{domain}/checkpoints.jsonl")}
    for snap in jl(OUT / f"{run}_{domain}/memory_snapshots.jsonl"):
        c = ck[snap["checkpoint_id"]]
        asker = c["asker"]["principal_id"]
        wiped = {
            r["turn_id"] for r in snap["records"] if r["state"] == "deleted" or "[removed]" in (r["text"] or "")
        }
        items = []
        for t in eps[c["episode_id"]].turns:
            if t.speaker_principal_id == asker and t.turn_id not in wiped:
                items.append({"memory_id": f"own-{t.turn_id}", "text": t.text, "principal_id": asker})
            if t.turn_id == c["as_of_turn_id"]:
                break
        out[snap["checkpoint_id"]] = items
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("domain")
    ap.add_argument("out")
    ap.add_argument("--variants", default="gsm,oracle,lc")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument(
        "--only", choices=["declined", "all_answer_expected", "utility", "privacy", "safety"], default="declined"
    )
    ap.add_argument("--limit", type=int, default=0, help="first N checkpoints only (smoke tests)")
    ap.add_argument("--ids", help="file with one checkpoint id per line (overrides --only)")
    args = ap.parse_args()

    base_agent, checkpoint_cls, _, _ = load_bench(ROOT)
    router = CapturingRouter(make_router(ROOT, BaselineAnswererConfig()))
    episodes, method_ckpts = load_for_method(ROOT / f"bench/data/{args.domain}")
    eps = {e.episode_id: e for e in episodes}
    mck = {c.checkpoint_id: c for c in method_ckpts}
    ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{args.domain}/checkpoints.jsonl")}
    ours = {p["checkpoint_id"]: p["output"] for p in jl(OUT / f"{args.run}_{args.domain}/predictions.jsonl")}
    lc_path = ROOT / f"outputs/{args.domain}_qwen38_27b_long_context/predictions.jsonl"
    if not lc_path.exists():  # education and household baselines were run from gsm-rebuild
        lc_path = ROOT / f"gsm-rebuild/outputs/baselines/{args.domain}_qwen38_27b_long_context/predictions.jsonl"
    lc = {p["checkpoint_id"]: p["output"] for p in jl(lc_path)}
    verdict = {
        r["checkpoint_id"]: r["judge"]
        for f in glob.glob(str(OUT / f"judged_{args.run}_{args.domain}/*/judge_scores.jsonl"))
        for r in jl(Path(f))
    }

    own: dict[str, list[dict[str, Any]]] = {}
    if {"gsm_own", "gsm_own_masked"} & set(args.variants.split(",")):
        own = own_messages(args.run, args.domain, eps)
    perm: dict[str, list[dict[str, Any]]] = {}
    if "perm_turns" in args.variants.split(","):
        perm = permitted_turns(args.run, args.domain, eps)
    permitted: dict[str, list[dict[str, Any]]] = {}
    if "all" in args.variants.split(","):
        permitted = permitted_items(args.run, args.domain)

    class _Agent(base_agent):  # type: ignore[misc, valid-type]
        def ingest(self, turn: Any) -> None:
            return None

    jobs = []
    wanted = set(Path(args.ids).read_text().split()) if args.ids else None
    for cid, c in ck.items():
        if wanted is not None:
            if cid not in wanted:
                continue
        elif (
            (args.only in ("declined", "all_answer_expected") and c["expected_action"] not in ANSWERING)
            or (args.only == "declined" and ours[cid]["action"] in ANSWERING)
            or (args.only in ("utility", "privacy", "safety") and c["query_type"] != args.only)
        ):
            continue
        gsm_items = parse_block(ours[cid]["memory_audit"]["prompt_context"]["text"])
        pats = [p for p in c["judge_spec"].get("include") or [] if isinstance(p, str)]
        oracle = [it for it in gsm_items if any(pattern_hits(it["text"], p) for p in pats)]
        for variant in args.variants.split(","):
            if variant == "oracle" and not oracle:
                continue
            for rep in range(args.repeats):
                jobs.append((cid, variant, rep, gsm_items, oracle, pats))

    def work(job: tuple[Any, ...]) -> dict[str, Any]:
        cid, variant, rep, gsm_items, oracle, pats = job
        c, m = ck[cid], mck[cid]
        agent = _Agent(llm_router=router)
        agent.reset(episode_for_bench(eps[c["episode_id"]]))
        cp = checkpoint_cls(
            checkpoint_id=m.checkpoint_id,
            episode_id=m.episode_id,
            as_of_turn_id=m.as_of_turn_id,
            asker_principal_id=m.asker_principal_id,
            asker_role=m.asker_role,
            query_type="",
            query_text=m.query_text,
        )
        try:
            if variant == "lc":
                out = agent._run_llm(
                    checkpoint=cp, retrieved_memory=[], rendered_memory_block_override=lc[cid]["prompt_memory_block"]
                )
            elif variant == "none":
                out = agent._run_llm(checkpoint=cp, retrieved_memory=[])
            elif variant == "no_wording":
                out = agent._run_llm(checkpoint=cp, retrieved_memory=[it for it in gsm_items if not _WORDING.search(it["text"])])
            elif variant == "gsm_own":
                out = agent._run_llm(checkpoint=cp, retrieved_memory=gsm_items + own.get(cid, []))
            elif variant == "gsm_own_masked":
                masked = [{**it, "text": _mask_values(it["text"])} for it in own.get(cid, [])]
                out = agent._run_llm(checkpoint=cp, retrieved_memory=gsm_items + masked)
            elif variant == "perm_turns":
                out = agent._run_llm(checkpoint=cp, retrieved_memory=perm.get(cid, []))
            elif variant == "all":
                out = agent._run_llm(checkpoint=cp, retrieved_memory=permitted.get(cid, []))
            else:
                out = agent._run_llm(checkpoint=cp, retrieved_memory=gsm_items if variant == "gsm" else oracle)
            answer, action, err = str(out.get("answer") or ""), str(out.get("action")), None
            block = str(out.get("prompt_memory_block") or "")
        except Exception as exc:  # recorded, not fatal
            answer, action, err, block = "", "error", f"{type(exc).__name__}: {str(exc)[:200]}", ""
        targets = [p for p in (c.get("leak_targets") or []) + (c["judge_spec"].get("not_include") or []) if isinstance(p, str)]
        return {
            "checkpoint_id": cid,
            "variant": variant,
            "rep": rep,
            "query_type": c["query_type"],
            "expected_action": c["expected_action"],
            "asker_role": c["asker"]["role"],
            "action": action,
            "answer": answer,
            "include_hits": sum(pattern_hits(answer, p) for p in pats),
            "answer_leak_hits": [p for p in targets if action in ANSWERING and pattern_hits(answer, p)],
            "context_leak_hits": [p for p in targets if pattern_hits(block, p)],
            "include_needed": len(pats),
            "judged_run_action": ours[cid]["action"],
            "judged_run_utility_correct": verdict[cid]["utility_correct"],
            "n_items": {
                "gsm": len(gsm_items),
                "oracle": len(oracle),
                "all": len(permitted.get(cid, [])),
                "gsm_own": len(gsm_items) + len(own.get(cid, [])),
                "perm_turns": len(perm.get(cid, [])),
                "gsm_own_masked": len(gsm_items) + len(own.get(cid, [])),
                "none": 0,
                "no_wording": sum(1 for it in gsm_items if not _WORDING.search(it["text"])),
            }.get(variant),
            "reasoning": getattr(router.local, "reasoning", ""),
            "error": err,
        }

    if args.limit:
        keep = list(dict.fromkeys(j[0] for j in jobs))[: args.limit]
        jobs = [j for j in jobs if j[0] in keep]
    print(f"{len(jobs)} answerer calls", flush=True)
    done = 0
    with open(args.out, "w") as fh, ThreadPoolExecutor(args.workers) as pool:
        for row in pool.map(work, jobs):
            fh.write(json.dumps(row) + "\n")
            done += 1
            if done % 50 == 0:
                print(f"{done}/{len(jobs)}", flush=True)


if __name__ == "__main__":
    main()
