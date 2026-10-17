"""Where GSM puts the messages that show an asker is entitled to an answer (analysis only; reads hidden labels).

For checkpoints the answerer declined with GSM's evidence but answered with long_context's transcript:
  1. Qwen reads the transcript (long_context's own memory block) and names the messages that establish the
     requester's involvement: assigned to it, asked to act, doing the task, owner of the case, or authorized
     to know. Those messages are the context long_context gives the answerer and GSM may not.
  2. Each named message is traced through GSM's run at that checkpoint: not stored (chatter, question, fact
     whose quotes left the sentence out), stored but not readable by the asker, no longer current, or readable
     and handed to the answerer.

Usage: entitlement_context.py RUN ANALYSIS_DIR OUT_DIR   (ANALYSIS_DIR/replay_vs_lc/ids_<domain>.txt)
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "gsm-rebuild/src"))
from gsm.providers.openai_compat import OpenAICompatClient  # noqa: E402

DOMAINS = ("medical", "office", "education", "household")
LC_PRED = {
    "medical": "outputs/medical_qwen38_27b_long_context/predictions.jsonl",
    "office": "outputs/office_qwen38_27b_long_context/predictions.jsonl",
    "education": "gsm-rebuild/outputs/baselines/education_qwen38_27b_long_context/predictions.jsonl",
    "household": "gsm-rebuild/outputs/baselines/household_qwen38_27b_long_context/predictions.jsonl",
}
SYSTEM = """You read the message history of a multi-party conversation and a requester's question. Name the messages
that show the requester is entitled to the answer: messages saying the requester is assigned to, responsible for,
or working on the matter asked about; asking the requester to do something about it; written by the requester while
doing that work; or explicitly authorizing the requester to know it. Do NOT list messages that only state the facts
being asked for.
Return ONLY {"messages": [<message numbers>], "why": "<one sentence>"} ({"messages": []} if none)."""
_LINE = re.compile(r"^Memory (\d+) \(speaker=([^)]*)\): (.*)$")


def jl(p: Path | str) -> list[dict]:
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def main(run: str, analysis_dir: str, out_dir: str) -> None:
    an, out = Path(analysis_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gate = ROOT / "gsm-rebuild/outputs/gatemem"
    client = OpenAICompatClient(
        os.environ["GSM_LLM_BASE_URL"], os.environ["GSM_LLM_MODEL"], api_key=os.environ["GSM_LLM_API_KEY"],
        timeout_s=900, max_retries=2,
    )
    summary = {}
    for d in DOMAINS:
        ids = (an / f"replay_vs_lc/ids_{d}.txt").read_text().split()
        if not ids:
            continue
        ck = {c["checkpoint_id"]: c for c in jl(ROOT / f"bench/data/{d}/checkpoints.jsonl")}
        eps = {e["episode_id"]: e for e in jl(ROOT / f"bench/data/{d}/episodes.jsonl")}
        lc = {p["checkpoint_id"]: p["output"] for p in jl(ROOT / LC_PRED[d])}
        snaps = {s["checkpoint_id"]: s["records"] for s in jl(gate / f"{run}_{d}/memory_snapshots.jsonl")}
        diag = {x["checkpoint_id"]: x for x in jl(gate / f"{run}_{d}/checkpoint_diag.jsonl")}
        kinds = {(e["episode_id"], e["turn_id"]): e for e in jl(gate / f"{run}_{d}/ingest_log.jsonl")}

        def work(cid: str) -> dict:
            c = ck[cid]
            ep = eps[c["episode_id"]]
            asker = c["asker"]["principal_id"]
            name = next(p["display_name"] for p in ep["entities"]["principals"] if p["principal_id"] == asker)
            idx = next(i for i, t in enumerate(ep["turns"]) if t["turn_id"] == c["as_of_turn_id"])
            turns = ep["turns"][: idx + 1][-300:]  # long_context's own window
            block = lc[cid]["prompt_memory_block"]
            user = (f"REQUESTER: {asker} ({c['asker']['role']}), named {name}\nQUESTION: {c['query_text']}\n\n"
                    f"MESSAGES:\n{block}")
            try:
                text, *_ = client.chat(SYSTEM, user, max_tokens=8000)
                m = re.search(r"\{.*\}", text, re.S)
                got = json.loads(m.group(0)) if m else {}
                nums = [int(n) for n in got.get("messages", []) if 1 <= int(n) <= len(turns)]
                why = str(got.get("why", ""))
            except Exception as exc:  # noqa: BLE001
                nums, why = [], f"failed: {type(exc).__name__}"
            recs = snaps[cid]
            allowed = {a["memory_id"] for a in diag[cid].get("allowed", [])}
            shown = set(diag[cid].get("evidence", []))
            traced = []
            for n in nums:
                t = turns[n - 1]
                rs = [r for r in recs if r["turn_id"] == t["turn_id"]]
                cur = [r for r in rs if r["state"] == "current"]
                if not rs:
                    k = kinds.get((c["episode_id"], t["turn_id"]), {})
                    stage = f"not stored ({k.get('kind', '?')})"
                elif any(r["memory_id"] in shown for r in cur):
                    stage = "readable and shown"
                elif any(r["memory_id"] in allowed for r in cur):
                    stage = "readable, not shown"
                elif cur:
                    stage = "stored, asker may not read"
                else:
                    stage = "no longer current (" + ",".join(sorted({r["state"] for r in rs})) + ")"
                traced.append({
                    "turn_id": t["turn_id"], "speaker": t["speaker"]["principal_id"], "by_asker":
                    t["speaker"]["principal_id"] == asker, "text": t["text"], "stage": stage,
                    "records": [{"state": r["state"], "text": r["text"], "readers": r["full_readers"]} for r in rs][:4],
                })
            return {"checkpoint_id": cid, "asker": c["asker"], "question": c["query_text"], "why": why,
                    "messages": traced}

        with ThreadPoolExecutor(12) as pool:
            rows = list(pool.map(work, ids))
        with (out / f"entitlement_{d}.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        stages = collections.Counter(m["stage"].split(" (")[0] if m["stage"].startswith("no longer") else m["stage"]
                                     for r in rows for m in r["messages"])
        per_ckpt = collections.Counter(
            "none named" if not r["messages"] else
            ("some shown" if any(m["stage"] == "readable and shown" for m in r["messages"]) else "none shown")
            for r in rows)
        summary[d] = {"checkpoints": len(rows), "messages": dict(stages.most_common()), "per_checkpoint": dict(per_ckpt),
                      "by_asker": sum(m["by_asker"] for r in rows for m in r["messages"])}
        print(d, summary[d], flush=True)
    (out / "entitlement_summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])
