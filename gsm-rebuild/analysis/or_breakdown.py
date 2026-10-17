"""Why is medical U low and OR high? Checkpoint-level breakdown (analysis only; reads hidden labels).

Compares GSM (a judged run) with the long_context baseline, which uses the same answerer and
judge, on the same checkpoints.
"""

from __future__ import annotations

import collections
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from bench.eval.metrics import pattern_hits  # noqa: E402

OUT = ROOT / "gsm-rebuild/outputs/gatemem"
BASE = ROOT / "outputs_qwen38-27b_judge"
ANSWERING = {"answer", "answer_redacted"}


def jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def evidence(output: dict) -> str:
    t = output["memory_audit"]["prompt_context"]["text"]
    return "" if t.strip() == "(none)" else re.sub(r"Memory \d+ \(speaker=[^)]*\): ", "", t)


def main(run: str = "v6bench", domain: str = "medical") -> None:
    ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{domain}/checkpoints.jsonl")}
    ours = {p["checkpoint_id"]: p["output"] for p in jl(OUT / f"{run}_{domain}/predictions.jsonl")}
    v_ours = {r["checkpoint_id"]: r["judge"] for f in glob.glob(str(OUT / f"judged_{run}_{domain}/*/judge_scores.jsonl")) for r in jl(Path(f))}
    v_lc = {r["checkpoint_id"]: r["judge"] for r in jl(BASE / f"{domain}_qwen38_27b_long_context/judge_scores.jsonl")}
    lc = {p["checkpoint_id"]: p["output"] for p in jl(ROOT / f"outputs/{domain}_qwen38_27b_long_context/predictions.jsonl")}

    # ---- utility
    util = [c for c in ck.values() if c["query_type"] == "utility"]
    tab = collections.Counter()
    by_role = collections.defaultdict(collections.Counter)
    for c in util:
        cid = c["checkpoint_id"]
        o, v = ours[cid], v_ours[cid]
        pats = [p for p in c["judge_spec"].get("include") or [] if isinstance(p, str)]
        ev = evidence(o)
        hits = sum(pattern_hits(ev, p) for p in pats)
        cover = "no evidence" if not ev else ("all needed facts" if hits == len(pats) else ("some needed facts" if hits else "no needed facts"))
        action = o["action"]
        outcome = "correct" if v["utility_correct"] else ("declined" if action not in ANSWERING else "answered wrong")
        tab[(outcome, cover)] += 1
        by_role[c["asker"]["role"]][outcome] += 1
        by_role[c["asker"]["role"]]["lc_correct"] += v_lc[cid]["utility_correct"]
    print(f"== {domain} utility ({len(util)} checkpoints): GSM outcome x evidence coverage (regex over the prompt block)")
    for k, n in sorted(tab.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {k[0]:15s} {k[1]}")
    print("  per asker role: GSM correct / declined / answered-wrong  | long_context correct")
    for role, t in sorted(by_role.items(), key=lambda x: -sum(v for k, v in x[1].items() if k != 'lc_correct')):
        n = t["correct"] + t["declined"] + t["answered wrong"]
        print(f"  {role:15s} n={n:3d}  {t['correct']:3d} / {t['declined']:3d} / {t['answered wrong']:3d}   | {t['lc_correct']:3d}")

    # ---- over-refusal: every checkpoint whose expected action answers
    exp_ans = [c for c in ck.values() if c["expected_action"] in ANSWERING]
    ort = collections.Counter()
    for c in exp_ans:
        cid = c["checkpoint_id"]
        if ours[cid]["action"] in ANSWERING:
            continue
        ev = evidence(ours[cid])
        ort[(c["query_type"], c["asker"]["role"], ours[cid]["action"], "evidence" if ev else "no evidence",
             "lc answered" if lc[cid]["action"] in ANSWERING else "lc declined")] += 1
    n_or = sum(ort.values())
    print(f"\n== {domain} over-refusals: {n_or} of {len(exp_ans)} answer-expected checkpoints")
    agg = collections.Counter()
    for (qt, role, act, ev, lcx), n in ort.items():
        agg[(act, ev)] += n
    for k, n in sorted(agg.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {k[0]:10s} {k[1]}")
    agg = collections.Counter()
    for (qt, role, act, ev, lcx), n in ort.items():
        agg[(role, lcx)] += n
    print("  by asker role (and whether long_context answered the same checkpoint):")
    for k, n in sorted(agg.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {k[0]:15s} {k[1]}")


if __name__ == "__main__":
    main(*sys.argv[1:])
