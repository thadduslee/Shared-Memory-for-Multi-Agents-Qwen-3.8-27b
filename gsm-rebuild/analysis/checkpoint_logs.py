"""Per-checkpoint replay logs for every episode of a judged GSM run (analysis only; reads hidden labels).

For each checkpoint: 1. the memory store at that moment, 2. the question, 3. the evidence GSM handed
to the answerer, 4. the answerer's output, 5. the benchmark's ground truth, 6. the judge's decision.

Usage: checkpoint_logs.py RUN_DIR JUDGED_DIR DOMAIN OUT_DIR
Writes OUT_DIR/<episode>.jsonl and OUT_DIR/<episode>.md, plus OUT_DIR/summary.json and summary.md.
"""

from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def cell(x: object) -> str:
    return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")


def verdict(qtype: str, j: dict) -> tuple[str, bool]:
    if qtype == "utility":
        return ("correct", True) if j["utility_correct"] else ("incorrect", False)
    if qtype == "privacy":
        return ("privacy leak", False) if j["privacy_leak"] else ("no leak", True)
    return ("deletion leak", False) if j["deletion_leak"] else ("no leak", True)


def main(run_dir: str, judged_dir: str, domain: str, out_dir: str) -> None:
    run, out = Path(run_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{domain}/checkpoints.jsonl")}
    eps = {e["episode_id"]: e for e in jl(ROOT / f"bench/data/{domain}/episodes.jsonl")}
    pred = {p["checkpoint_id"]: p["output"] for p in jl(run / "predictions.jsonl")}
    snap = {s["checkpoint_id"]: s for s in jl(run / "memory_snapshots.jsonl")}
    diag = {d["checkpoint_id"]: d for d in jl(run / "checkpoint_diag.jsonl")}
    judge = {r["checkpoint_id"]: r["judge"] for f in glob.glob(f"{judged_dir}/*/judge_scores.jsonl") for r in jl(Path(f))}

    by_ep: dict[str, list[str]] = collections.defaultdict(list)
    for cid in pred:
        by_ep[ck[cid]["episode_id"]].append(cid)

    summary = []
    for eid, ep in eps.items():
        cids = by_ep.get(eid, [])
        if not cids:
            continue
        turn_text = {t["turn_id"]: t["text"] for t in ep["turns"]}
        rows = []
        for cid in cids:
            c, o, j = ck[cid], pred[cid], judge[cid]
            allowed = {a["memory_id"]: a["view"] for a in diag[cid].get("allowed", [])}
            shown = set(diag[cid].get("evidence", []))
            records = []
            for r in snap[cid]["records"]:
                r = dict(r)
                r["asker_can_read"] = allowed.get(r["memory_id"])
                r["shown_to_answerer"] = r["memory_id"] in shown
                records.append(r)
            ev = o["memory_audit"]["prompt_context"]["text"]
            v, ok = verdict(c["query_type"], j)
            rows.append(
                {
                    "checkpoint_id": cid,
                    "as_of_turn_id": c["as_of_turn_id"],
                    "asker": c["asker"],
                    "query_type": c["query_type"],
                    "attack_type": c.get("attack_type"),
                    "1_memory_state": {"counts": dict(collections.Counter(r["state"] for r in records)), "records": records},
                    "2_question": c["query_text"],
                    "3_evidence": [] if ev.strip() == "(none)" else ev.split("\n"),
                    "4_answerer_output": {"action": o["action"], "answer": o["answer"]},
                    "5_ground_truth": {
                        "expected_action": c["expected_action"],
                        "must_include": c["judge_spec"].get("include", []),
                        "must_not_include": c["judge_spec"].get("not_include", []),
                        "leak_targets": c.get("leak_targets", []),
                        "reason": c["judge_spec"].get("notes", ""),
                    },
                    "6_judge": {
                        "verdict": v,
                        "pass": ok,
                        "action_correct": j.get("action_correct"),
                        "utility_correct": j.get("utility_correct"),
                        "privacy_leak": j.get("privacy_leak"),
                        "deletion_leak": j.get("deletion_leak"),
                        "reason": j.get("reason", ""),
                        "missed_patterns": j.get("missed_patterns", []),
                        "leak_hits": j.get("leak_hits", []),
                        "llm_judged": j.get("llm_judged"),
                        "rescued_by_llm": j.get("rescued_by_llm", False),
                    },
                }
            )
        with (out / f"{eid}.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        write_md(out / f"{eid}.md", eid, ep, rows, turn_text)
        counts = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            counts[r["query_type"]][1] += 1
            counts[r["query_type"]][0] += r["6_judge"]["pass"]
        summary.append({"episode_id": eid, "turns": len(ep["turns"]), "checkpoints": len(rows), **{k: v for k, v in counts.items()}})
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"{domain}: {len(summary)} episodes written to {out}")


def write_md(path: Path, eid: str, ep: dict, rows: list[dict], turn_text: dict) -> None:
    md = [f"# Replay: {eid}", "", f"{len(ep['turns'])} messages, {len(rows)} checkpoints. GSM v13 configuration; the "
          "GateMem baselines' answerer; Qwen3.8-27B judge.", "",
          "Memory states: **current**, **superseded** (replaced under the same key), **retired** (ended without "
          "replacement), **deleted** (tombstoned; GSM scrubbed the text). `asker`: whether this checkpoint's asker "
          "may read the record (full / redacted); `shown`: handed to the answerer.", "",
          "| # | type | asker | expected | answered | judge |", "|---|---|---|---|---|---|"]
    for r in rows:
        n = r["checkpoint_id"].split("_ckpt_")[1]
        md.append(f"| [{n}](#ckpt-{n}) | {r['query_type']} | {r['asker']['principal_id']} ({r['asker']['role']}) | "
                  f"{r['5_ground_truth']['expected_action']} | {r['4_answerer_output']['action']} | {r['6_judge']['verdict']} |")
    for r in rows:
        n = r["checkpoint_id"].split("_ckpt_")[1]
        st, a, g, j = r["1_memory_state"], r["4_answerer_output"], r["5_ground_truth"], r["6_judge"]
        md += ["", "---", "", f"## <a id=\"ckpt-{n}\"></a>Checkpoint {n} · {r['query_type']}"
               + (f" · attack: {r['attack_type']}" if r["attack_type"] else ""), "",
               f"Asked by **{r['asker']['principal_id']}** ({r['asker']['role']}) after message {r['as_of_turn_id']}: "
               f"“{cell(turn_text.get(r['as_of_turn_id'], ''))[:160]}”", "",
               "### 1. Memory state", "", "Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(st["counts"].items())), "",
               "| turn | speaker | state | key | text | full readers | summary readers | restricted from | asker | shown |",
               "|---|---|---|---|---|---|---|---|---|---|"]
        for x in st["records"]:
            md.append(f"| {cell(x['turn_id'])} | {cell(x['speaker'])} | {x['state']} | {cell(x['key'])} | "
                      f"{cell(x['text'] if x['text'] is not None else '(scrubbed)')} | {cell(', '.join(x['full_readers']))} | "
                      f"{cell(', '.join(x['summary_readers']))} | {cell(', '.join(x['restricted_from']))} | "
                      f"{cell(x['asker_can_read'] or '')} | {'✓' if x['shown_to_answerer'] else ''} |")
        md += ["", "### 2. Question", "", f"> {cell(r['2_question'])}", "",
               f"### 3. Evidence handed to the answerer ({len(r['3_evidence'])} records)", ""]
        md += [f"- {cell(e)}" for e in r["3_evidence"]] or ["(none)"]
        md += ["", "### 4. Answerer output", "", f"**{a['action']}**: {cell(a['answer'])}", "",
               "### 5. Ground truth", "", f"- Expected action: **{g['expected_action']}**",
               f"- Must include: {', '.join('`' + cell(x) + '`' for x in g['must_include']) or '(none)'}",
               f"- Must not include: {', '.join('`' + cell(x) + '`' for x in g['must_not_include']) or '(none)'}",
               f"- Leak targets: {', '.join('`' + cell(x) + '`' for x in g['leak_targets']) or '(none)'}",
               f"- Reason: {cell(g['reason'])}", "",
               "### 6. Judge", "", f"- Verdict: **{j['verdict']}** (action correct: {j['action_correct']})",
               f"- Reason: {cell(j['reason'])}",
               f"- Missed required patterns: {', '.join('`' + cell(x) + '`' for x in j['missed_patterns']) or '(none)'}",
               f"- Forbidden patterns found: {', '.join('`' + cell(x) + '`' for x in j['leak_hits']) or '(none)'}",
               f"- LLM judged: {j['llm_judged']}" + (" (LLM overruled the pattern check)" if j["rescued_by_llm"] else "")]
    path.write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
