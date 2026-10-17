"""Rescore any GateMem predictions.jsonl with govmem's judge, restricted to the episodes of a govmem run, so a
baseline and govmem are compared by the same instrument on the same checkpoints.

    python -m govmem.eval.rescore --run-dir runs/smoke_medical_2ep --name long_context \
        --predictions ../outputs/medical_qwen38_27b_long_context/predictions.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from govmem.adapters.gatemem.data import GateMemData, annotation, read_jsonl, visible_checkpoint, write_jsonl
from govmem.config import Settings
from govmem.eval.judge import aggregate, judge_all
from govmem.llm import ChatClient, ReplyCache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--rule-judge", action="store_true")
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    manifest = json.loads((run / "run.json").read_text())
    settings = Settings.from_env()
    data = GateMemData(settings.gatemem_repo / "bench" / "data", manifest["domain"])
    cps = {c["checkpoint_id"]: c for c in data.checkpoints if c["episode_id"] in set(manifest["episodes"])}
    preds = []
    for row in read_jsonl(Path(args.predictions)):
        c = cps.get(row.get("checkpoint_id"))
        if c is None:
            continue
        v = visible_checkpoint(c)
        preds.append({"checkpoint_id": c["checkpoint_id"], "episode_id": c["episode_id"],
                      "as_of_turn_id": v["as_of_turn_id"], "asker": v["asker"], "query_text": v.get("query_text"),
                      "output": row["output"]})
    missing = sorted(set(cps) - {p["checkpoint_id"] for p in preds})
    anns = {cid: annotation(c) for cid, c in cps.items()}
    out = run / "baselines" / args.name
    out.mkdir(parents=True, exist_ok=True)
    verdicts = judge_all(preds, anns, chat=None if args.rule_judge else ChatClient(settings.judge),
                         cache=ReplyCache(run / "cache" / "judge.sqlite"), repo=settings.gatemem_repo)
    write_jsonl(out / "verdicts.jsonl", verdicts)
    scores = aggregate(verdicts) | {"n_missing": len(missing)}
    (out / "scores.json").write_text(json.dumps(scores, indent=1))
    print(json.dumps({"name": args.name, **{k: round(v, 3) if isinstance(v, float) else v for k, v in scores.items()}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
