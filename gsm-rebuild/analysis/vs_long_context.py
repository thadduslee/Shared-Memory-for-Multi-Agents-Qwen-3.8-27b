"""Checkpoint-level comparison of GSM against the long_context baseline (analysis only; reads hidden labels).

For every checkpoint, both systems' verdicts under the same Qwen judge. Each disagreement is explained:
  GSM fails, long_context passes  - utility: the reason from utility_reasons.py (M*/W*/A*/J);
                                    privacy/deletion: the category from failure_logs.py, plus whether the
                                    forbidden text was in GSM's evidence
  long_context fails, GSM passes  - utility: what long_context did (refused, redacted, answered wrong);
                                    privacy/deletion: whether GSM's evidence held the forbidden text at all
                                    (no: GSM's access control or deletion kept it out; yes: GSM's answerer held back)

Usage: vs_long_context.py ANALYSIS_DIR GSM_RUN_PREFIX OUT_DIR
  ANALYSIS_DIR holds failure_logs/ (failures_<d>.jsonl, utility/utility_reasons_<d>.jsonl) from the GSM run.
"""

from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from bench.eval.metrics import pattern_hits  # noqa: E402

DOMAINS = ("medical", "office", "education", "household")
LC_JUDGE = {
    "medical": "outputs_qwen38-27b_judge/medical_qwen38_27b_long_context/judge_scores.jsonl",
    "office": "outputs_qwen38-27b_judge/office_qwen38_27b_long_context/judge_scores.jsonl",
    "education": "gsm-rebuild/outputs/baselines/judged_education_main/education_qwen38_27b_long_context/judge_scores.jsonl",
    "household": "gsm-rebuild/outputs/baselines/judged_household_main/household_qwen38_27b_long_context/judge_scores.jsonl",
}
LC_PRED = {
    "medical": "outputs/medical_qwen38_27b_long_context/predictions.jsonl",
    "office": "outputs/office_qwen38_27b_long_context/predictions.jsonl",
    "education": "gsm-rebuild/outputs/baselines/education_qwen38_27b_long_context/predictions.jsonl",
    "household": "gsm-rebuild/outputs/baselines/household_qwen38_27b_long_context/predictions.jsonl",
}
QT = {"utility": "utility", "privacy": "privacy", "safety": "deletion"}


def jl(p: Path | str) -> list[dict]:
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def passed(qt: str, j: dict) -> bool:
    if qt == "utility":
        return bool(j["utility_correct"])
    return not j["privacy_leak"] if qt == "privacy" else not j["deletion_leak"]


def strs(xs: list) -> list[str]:
    return [x for x in xs if isinstance(x, str)]


def main(analysis_dir: str, prefix: str, out_dir: str) -> None:
    an, out = Path(analysis_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gate = ROOT / "gsm-rebuild/outputs/gatemem"
    summary: dict[str, dict] = {}
    for d in DOMAINS:
        ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{d}/checkpoints.jsonl")}
        gj = {r["checkpoint_id"]: r["judge"] for f in glob.glob(str(gate / f"judged_{prefix}_{d}/*/judge_scores.jsonl")) for r in jl(f)}
        lj = {r["checkpoint_id"]: r["judge"] for r in jl(ROOT / LC_JUDGE[d])}
        gp = {p["checkpoint_id"]: p["output"] for p in jl(gate / f"{prefix}_{d}/predictions.jsonl")}
        lp = {p["checkpoint_id"]: p["output"] for p in jl(ROOT / LC_PRED[d])}
        ureason = {r["checkpoint_id"]: r for r in jl(an / f"failure_logs/utility/utility_reasons_{d}.jsonl")}
        fcat = {r["checkpoint_id"]: r for r in jl(an / f"failure_logs/failures_{d}.jsonl")}
        quad: collections.Counter = collections.Counter()
        why: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
        rows = []
        for cid, c in ck.items():
            qt = c["query_type"]
            if cid not in gj or cid not in lj:
                continue
            g, lc = passed(qt, gj[cid]), passed(qt, lj[cid])
            side = {(True, True): "both pass", (False, False): "both fail", (False, True): "GSM only fails",
                    (True, False): "LC only fails"}[(g, lc)]
            quad[(QT[qt], side)] += 1
            if side not in ("GSM only fails", "LC only fails"):
                continue
            g_ev = gp[cid]["memory_audit"]["prompt_context"]["text"]
            forbidden = list(dict.fromkeys(strs(c["judge_spec"].get("not_include", [])) + strs(c.get("leak_targets", []))))
            if side == "GSM only fails":
                if qt == "utility":
                    r = ureason.get(cid)
                    reason = f"{r['reason']} {r['reason_name']}" if r else "unclassified"
                else:
                    hits = strs(gj[cid].get("leak_hits") or []) or forbidden
                    in_ev = any(pattern_hits(g_ev, h) for h in hits)
                    f = fcat.get(cid)
                    reason = (f"{f['category']} {f['category_name']}" if f else "unclassified") + (
                        " (forbidden text in GSM evidence)" if in_ev else " (not in GSM evidence: answerer produced it)")
            else:  # LC only fails
                if qt == "utility":
                    act = lp[cid]["action"]
                    reason = f"long_context {act}" if act != "answer" else "long_context answered but missed/misstated facts"
                else:
                    hits = strs(lj[cid].get("leak_hits") or []) or forbidden
                    in_ev = any(pattern_hits(g_ev, h) for h in hits)
                    reason = ("GSM evidence held it; GSM's answerer withheld it" if in_ev
                              else "GSM kept it out of the evidence (access control / deletion)")
            why[(QT[qt], side)][reason] += 1
            rows.append({
                "checkpoint_id": cid, "query_type": QT[qt], "attack_type": c.get("attack_type"), "side": side,
                "reason": reason, "asker": c["asker"], "question": c["query_text"],
                "expected_action": c["expected_action"], "must_include": strs(c["judge_spec"].get("include", [])),
                "must_not_include": forbidden, "gsm": {"action": gp[cid]["action"], "answer": gp[cid]["answer"],
                "judge": gj[cid].get("reason", "")}, "long_context": {"action": lp[cid]["action"],
                "answer": lp[cid]["answer"], "judge": lj[cid].get("reason", "")},
            })
        with (out / f"vs_long_context_{d}.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        summary[d] = {"quadrants": {f"{a}|{b}": n for (a, b), n in sorted(quad.items())},
                      "why": {f"{a}|{b}": dict(c.most_common()) for (a, b), c in sorted(why.items())}}
        write_md(out / f"vs_long_context_{d}.md", d, quad, why, rows)
    (out / "vs_long_context_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


def cell(x: object) -> str:
    return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")


def write_md(path: Path, d: str, quad: collections.Counter, why: dict, rows: list[dict]) -> None:
    md = [f"# GSM vs long_context: {d}", "", "| type | both pass | GSM only fails | LC only fails | both fail |",
          "|---|---|---|---|---|"]
    for qt in ("utility", "privacy", "deletion"):
        md.append(f"| {qt} | " + " | ".join(str(quad[(qt, s)]) for s in
                                             ("both pass", "GSM only fails", "LC only fails", "both fail")) + " |")
    for (qt, side), c in sorted(why.items()):
        md += ["", f"## {qt}: {side}", ""] + [f"- {n} × {r}" for r, n in c.most_common()]
    md += ["", "## Checkpoints", ""]
    for r in rows:
        md += [f"### {r['checkpoint_id']} · {r['query_type']} · {r['side']}", "",
               f"- Asker: {r['asker']['principal_id']} ({r['asker']['role']}); expected **{r['expected_action']}**",
               f"- Question: {cell(r['question'])}",
               f"- Must include: {', '.join('`' + cell(x) + '`' for x in r['must_include']) or '(none)'}",
               f"- Must not include: {', '.join('`' + cell(x) + '`' for x in r['must_not_include']) or '(none)'}",
               f"- GSM ({r['gsm']['action']}): {cell(r['gsm']['answer'])[:600]}",
               f"- long_context ({r['long_context']['action']}): {cell(r['long_context']['answer'])[:600]}",
               f"- Why: {cell(r['reason'])}", ""]
    path.write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
