"""long_context in continuous mode: a whole GateMem domain as one conversation, sliding token window.

The benchmark's long_context baseline (bench/agents/long_context.py) resets at every episode and shows the
answerer the episode's last 300 turns. Here the episodes of a domain are one conversation (file order, the same
merge GSM's --continuous uses), and each checkpoint gets the most recent turns up to and including its own that
fit the model's context: max_model_len - max_output_tokens - the rest of the prompt - a safety margin, counted
with the model's own tokenizer.

The answerer is the benchmark's own (BaseMemoryAgent._run_llm, same prompt and router settings as the
baselines and GSM: Qwen3.8-27B, temperature 0.2, 16384 output tokens, thinking at the server default). The
memory block is rendered exactly as long_context renders it ("Memory i (speaker=...): text").

Usage: continuous_long_context.py DOMAIN OUT_DIR [--max-model-len 65536] [--workers 12] [--limit N]
Writes OUT_DIR/predictions.jsonl (resumable) and OUT_DIR/run_summary.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gsm-rebuild/src"))

from gsm.eval.gatemem.baseline_answerer import (  # noqa: E402
    BaselineAnswererConfig,
    episode_for_bench,
    load_bench,
    make_router,
)
from gsm.eval.gatemem.continuous import merge_domain  # noqa: E402
from gsm.eval.gatemem.sanitize import load_for_method  # noqa: E402

SAFETY_MARGIN = 512  # chat-template tokens and rounding


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("domain")
    ap.add_argument("out_dir")
    ap.add_argument("--max-model-len", type=int, default=65536)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from transformers import AutoTokenizer

    cfg = BaselineAnswererConfig()
    tok = AutoTokenizer.from_pretrained(cfg.model)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base_agent, checkpoint_cls, _, _ = load_bench(ROOT)
    router = make_router(ROOT, cfg)

    episodes, checkpoints = load_for_method(ROOT / f"bench/data/{args.domain}")
    merged, ckpts, report = merge_domain(episodes, checkpoints)
    position = {t.turn_id: i for i, t in enumerate(merged.turns)}
    bench_episode = episode_for_bench(merged)

    class _Agent(base_agent):  # type: ignore[misc, valid-type]
        def ingest(self, turn: Any) -> None:  # the window is built per checkpoint below
            return None

    def ntok(text: str) -> int:
        return len(tok.encode(text, add_special_tokens=False))

    # Token cost of every turn as long_context renders it (the "Memory i" number costs a few tokens; the margin
    # covers it).
    line_tokens = [ntok(f"Memory 0000 (speaker={t.speaker_principal_id}): {t.text.strip()}") + 1 for t in merged.turns]

    probe = _Agent(llm_router=router)
    probe.reset(bench_episode)
    template = probe._load_query_template()
    system_t, user_t = probe._split_system_user(template)

    def prompt_overhead(cp: Any) -> int:
        fmt = {
            "asker_principal_id": cp.asker_principal_id,
            "asker_role": cp.asker_role,
            "relationship_facts_block": probe._format_relationship_facts(cp),
            "retrieved_memory_block": "",
            "query_text": cp.query_text,
            "domain_label": "",
            "global_access_policy_block": "",
        }
        from bench.domains import detect_domain_from_episode, get_domain_label, get_query_policy_block

        dom = detect_domain_from_episode(bench_episode)
        fmt["domain_label"] = get_domain_label(dom)
        fmt["global_access_policy_block"] = get_query_policy_block(dom)
        return ntok(system_t.format(**fmt)) + ntok(user_t.format(**fmt))

    done: set[str] = set()
    pred_path = out / "predictions.jsonl"
    if pred_path.exists():
        done = {json.loads(x)["checkpoint_id"] for x in pred_path.read_text().splitlines() if x.strip()}
    jobs = [c for c in ckpts if c.checkpoint_id not in done]
    if args.limit:
        jobs = jobs[: args.limit]
    lock = threading.Lock()
    stats: list[dict[str, Any]] = []

    def work(c: Any) -> None:
        cp = checkpoint_cls(
            checkpoint_id=c.checkpoint_id,
            episode_id=c.episode_id,
            as_of_turn_id=c.as_of_turn_id,
            asker_principal_id=c.asker_principal_id,
            asker_role=c.asker_role,
            query_type="",
            query_text=c.query_text,
        )
        agent = _Agent(llm_router=router)
        agent.reset(bench_episode)
        budget = args.max_model_len - cfg.max_output_tokens - prompt_overhead(cp) - SAFETY_MARGIN
        end = position[c.as_of_turn_id]  # inclusive: only what was said up to the checkpoint
        start, used = end + 1, 0
        while start > 0 and used + line_tokens[start - 1] <= budget:
            start -= 1
            used += line_tokens[start]
        memory = [
            {"record_id": t.turn_id, "turn_id": t.turn_id, "principal_id": t.speaker_principal_id, "text": t.text}
            for t in merged.turns[start : end + 1]
        ]
        t0 = time.perf_counter()
        try:
            res = agent._run_llm(checkpoint=cp, retrieved_memory=memory)
            res.pop("retrieved_memory", None)  # the prompt block (memory_audit) is the record of what was shown
            err = None
        except Exception as exc:  # recorded; scored as a failure
            res, err = {"action": "refuse", "answer": "", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, exc
        row = {
            "checkpoint_id": c.checkpoint_id,
            "output": res,
            "window": {"first_turn": merged.turns[start].turn_id if start <= end else None, "last_turn": c.as_of_turn_id,
                       "turns": end + 1 - start, "turns_available": end + 1, "memory_tokens": used, "budget": budget},
            "latency_s": round(time.perf_counter() - t0, 2),
            "error": str(err) if err else None,
        }
        with lock:
            with pred_path.open("a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            stats.append(row["window"] | {"error": bool(err)})
            if len(stats) % 25 == 0:
                print(f"{len(stats)}/{len(jobs)}", flush=True)

    started = time.perf_counter()
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(work, jobs))
    rows = [json.loads(x) for x in pred_path.read_text().splitlines() if x.strip()]
    wins = [r["window"] for r in rows]
    summary = {
        "domain": args.domain,
        "method": "long_context (continuous, sliding token window)",
        "merge": report.__dict__,
        "max_model_len": args.max_model_len,
        "max_output_tokens": cfg.max_output_tokens,
        "checkpoints": len(rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "window_truncated": sum(1 for w in wins if w["turns"] < w["turns_available"]),
        "mean_window_turns": round(sum(w["turns"] for w in wins) / max(1, len(wins)), 1),
        "mean_turns_available": round(sum(w["turns_available"] for w in wins) / max(1, len(wins)), 1),
        "wall_seconds_this_session": round(time.perf_counter() - started, 1),
    }
    (out / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
