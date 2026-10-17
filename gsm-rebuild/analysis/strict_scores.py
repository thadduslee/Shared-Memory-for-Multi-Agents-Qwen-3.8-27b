"""Answer-level and strict end-to-end scores for every system, via GateMem's official scorer.

For each system and domain:
  official   bench.eval.scorer.score_predictions, unchanged (rule-based): compliance_utility_score (answer-level
             MGS) and compliance_utility_e2e_score (strict: a forbidden pattern in the answer OR in the prompt
             context exposed to the answer model, read from memory_audit.prompt_context / prompt_memory_block)
  qwen       the Qwen3.8-27B judge's verdicts (our headline MGS); qwen_strict combines them with the official
             scorer's per-checkpoint context-exposure flags (privacy_context_leak / deletion_context_leak)

Usage: strict_scores.py OUT_DIR
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from bench.eval.scorer import score_predictions  # noqa: E402

DOMAINS = ("medical", "office", "education", "household")
BASELINES = ("long_context", "rag_naive", "rag_policy", "a_mem", "mem0", "remem_i", "remem_s")
GSM_RUNS = {"GSM v13": {"medical": "v13bench", "office": "v13bench", "education": "v13", "household": "v13"},
            "GSM v20auth": dict.fromkeys(DOMAINS, "v20auth"),
            "GSM v22hist": dict.fromkeys(DOMAINS, "v22hist")}
GATE = ROOT / "gsm-rebuild/outputs/gatemem"
BASE = ROOT / "gsm-rebuild/outputs/baselines"


def jl(p: Path | str) -> list[dict]:
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def sources(system: str, d: str) -> tuple[Path, dict[str, dict]]:
    """predictions.jsonl and the Qwen judge's per-checkpoint verdicts."""
    if system.startswith("GSM"):
        run = GSM_RUNS[system][d]
        files = glob.glob(str(GATE / f"judged_{run}_{d}/*/judge_scores.jsonl"))
        return GATE / f"{run}_{d}/predictions.jsonl", {r["checkpoint_id"]: r["judge"] for f in files for r in jl(f)}
    name = f"{d}_qwen38_27b_{system}"
    if d in ("medical", "office"):
        pred, judge = ROOT / f"outputs/{name}/predictions.jsonl", ROOT / f"outputs_qwen38-27b_judge/{name}/judge_scores.jsonl"
    else:
        pred = BASE / f"{name}/predictions.jsonl"
        tag = {"education_qwen38_27b_mem0": "mem0", "household_qwen38_27b_remem_s": "remem_s"}.get(name, "main")
        judge = BASE / f"judged_{d}_{tag}/{name}/judge_scores.jsonl"
    return pred, {r["checkpoint_id"]: r["judge"] for r in jl(judge)}


def rate(xs: list[bool]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in DOMAINS:
        episodes = jl(ROOT / f"bench/data/{d}/episodes.jsonl")
        checkpoints = jl(ROOT / f"bench/data/{d}/checkpoints.jsonl")
        qtype = {c["checkpoint_id"]: c["query_type"] for c in checkpoints}
        for system in (*BASELINES, *GSM_RUNS):
            pred_path, qwen = sources(system, d)
            preds = jl(pred_path)
            scores, summary = score_predictions(episodes=episodes, checkpoints=checkpoints, predictions=preds)
            ctx = {s["checkpoint_id"]: s for s in scores}
            u, a, f, ae, fe = [], [], [], [], []
            for cid, qt in qtype.items():
                j, s = qwen.get(cid), ctx.get(cid)
                if j is None or s is None:  # missing prediction: counts as a failure everywhere
                    j = {"utility_correct": False, "privacy_leak": True, "deletion_leak": True}
                    s = {"privacy_context_leak": True, "deletion_context_leak": True}
                if qt == "utility":
                    u.append(bool(j["utility_correct"]))
                elif qt == "privacy":
                    a.append(bool(j["privacy_leak"]))
                    ae.append(bool(j["privacy_leak"]) or bool(s.get("privacy_context_leak")))
                else:
                    f.append(bool(j["deletion_leak"]))
                    fe.append(bool(j["deletion_leak"]) or bool(s.get("deletion_context_leak")))
            U, A, F, AE, FE = rate(u), rate(a), rate(f), rate(ae), rate(fe)
            rows.append({
                "domain": d, "system": system, "n": len(preds),
                "qwen": {"U": U, "A": A, "F": F, "MGS": U * (1 - A) * (1 - F)},
                "qwen_strict": {"U": U, "A_e2e": AE, "F_e2e": FE, "MGS_e2e": U * (1 - AE) * (1 - FE)},
                "official": {k: summary.get(k) for k in (
                    "utility_accuracy", "privacy_leakage_rate", "deletion_leakage_rate",
                    "privacy_context_leakage_rate", "deletion_context_leakage_rate",
                    "privacy_e2e_leakage_rate", "deletion_e2e_leakage_rate",
                    "compliance_utility_score", "compliance_utility_e2e_score")},
            })
            r = rows[-1]
            print(f"{d:9s} {system:12s} qwen MGS {r['qwen']['MGS']:.3f} | qwen strict {r['qwen_strict']['MGS_e2e']:.3f} | "
                  f"official MGS {r['official']['compliance_utility_score']:.3f} e2e {r['official']['compliance_utility_e2e_score']:.3f}",
                  flush=True)
    (out / "strict_scores.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])
