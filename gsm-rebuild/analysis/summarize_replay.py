"""Summarize replay_answerer.py outputs: answer rates, fact coverage and leaks per variant."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ANSWERING = {"answer", "answer_redacted"}


def main(paths: list[str]) -> None:
    rows = [json.loads(x) for p in paths for x in Path(p).read_text().splitlines() if x.strip()]
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["query_type"], r["variant"])].append(r)
    print(f"{'query_type':9s} {'variant':7s} {'n':>4s} {'answered':>9s} {'all facts':>10s} {'ans.leak':>9s} {'ctx.leak':>9s} {'errors':>6s}")
    for (qt, var), rs in sorted(by.items()):
        n = len(rs)
        answered = sum(r["action"] in ANSWERING for r in rs)
        full = sum(r["action"] in ANSWERING and r["include_needed"] and r["include_hits"] == r["include_needed"] for r in rs)
        aleak = sum(bool(r.get("answer_leak_hits")) for r in rs)
        cleak = sum(bool(r.get("context_leak_hits")) for r in rs)
        err = sum(r["action"] == "error" for r in rs)
        print(f"{qt:9s} {var:7s} {n:4d} {answered / n:9.0%} {full / n:10.0%} {aleak / n:9.0%} {cleak / n:9.0%} {err:6d}")
    # consistency of repeated identical prompts
    reps = collections.defaultdict(list)
    for r in rows:
        if r["variant"] == "gsm":
            reps[r["checkpoint_id"]].append(r["action"] in ANSWERING)
    multi = [v for v in reps.values() if len(v) > 1]
    if multi:
        mixed = sum(1 for v in multi if 0 < sum(v) < len(v))
        print(f"\nidentical GSM prompt repeated: {len(multi)} checkpoints, answer/decline disagree across repeats on {mixed}")
    # per role (answered rate)
    roles = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        roles[(r["asker_role"], r["variant"])]["n"] += 1
        roles[(r["asker_role"], r["variant"])]["ans"] += r["action"] in ANSWERING
    print("\nanswered rate by asker role:")
    for (role, var), c in sorted(roles.items()):
        print(f"  {role:15s} {var:7s} {c['ans']:3d}/{c['n']:3d}")


if __name__ == "__main__":
    main(sys.argv[1:])
