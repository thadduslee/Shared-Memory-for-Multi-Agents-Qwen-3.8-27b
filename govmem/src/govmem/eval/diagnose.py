"""Why each checkpoint failed (eval-side only; reads annotations and the memory as its owner).

Utility: for every required pattern the answer missed, find the furthest stage it reached at the query's as-of
point, and label the checkpoint by its worst missed pattern:
  in_evidence           -- released to the answerer; the answerer still failed
  readable_not_released -- the asker could open a record holding it, but it was not released (ranked mode)
  stored_not_readable   -- a current record holds it, but the asker cannot open it
  superseded            -- only a superseded record holds it
  not_in_memory         -- no live record holds it (never stored, or deleted)
Privacy / deletion exposures: for every evidence record that matches a forbidden pattern, its speaker, source turn,
and for deletions whether it was written before or after the episode's deletion turns.

Memory state is the end of the episode; views and supersession are read relative to the as-of turn where the
store records it (created_seq, superseded_seq). Views are end-state, so a later revocation reads as unreadable.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from govmem.adapters.gatemem.data import read_jsonl, write_jsonl
from govmem.core.store import Store
from govmem.eval.judge import _list, pattern_hits

STAGES = ["in_evidence", "readable_not_released", "stored_not_readable", "superseded", "not_in_memory"]


def _episode_index(store: Store) -> dict[str, Any]:
    recs = []
    for r in store.records(current_only=False, alive_only=True):
        recs.append({"id": r.id, "text": store.text_owner(r.id) or "", "created_seq": r.created_seq,
                     "superseded_seq": r.superseded_seq, "speaker": r.speaker_id, "turn": r.source_turn,
                     "views": store.views(r.id)})
    deletes = sorted({store.turn_seq(row["turn"]) or 0 for row in
                      store.q("SELECT turn FROM _op_log WHERE op='delete' AND outcome='applied'")})
    return {"records": recs, "delete_seqs": deletes}


def diagnose(run: Path, episode_ids: list[str], verdicts: list[dict[str, Any]], data_cps: dict[str, dict]) -> dict:
    by_ep: dict[str, dict[str, Any]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    for eid in episode_ids:
        dbp = run / "episodes" / eid / "memory.db"
        if not dbp.exists():
            continue
        store = Store(str(dbp))
        idx = _episode_index(store)
        idx["seq"] = {r["turn_id"]: r["seq"] for r in store.q("SELECT turn_id, seq FROM turns")}
        idx["n_delete_ops"] = len(idx["delete_seqs"])
        store.conn.close()
        by_ep[eid] = idx
        for row in read_jsonl(run / "episodes" / eid / "evidence.jsonl"):
            evidence[row["checkpoint"]["checkpoint_id"]] = row["result"]["evidence"]
    rows = []
    for v in verdicts:
        cid, qt = v["checkpoint_id"], v["query_type"]
        failed = (qt == "utility" and not v["utility_correct"]) or v["privacy_leak"] or v["deletion_leak"] \
            or v["context_exposed"]
        if not failed or v.get("episode_id") not in by_ep:
            continue
        cp, idx = data_cps[cid], by_ep[v["episode_id"]]
        asof = idx["seq"].get(cp["as_of_turn_id"], 0)
        asker = cp["asker"]["principal_id"]
        ev = evidence.get(cid, [])
        ev_text = " \n ".join(e["text"] for e in ev)
        live = [r for r in idx["records"] if r["created_seq"] <= asof]
        out: dict[str, Any] = {"checkpoint_id": cid, "episode_id": v["episode_id"], "query_type": qt,
                               "asker_role": cp["asker"]["role"], "action": v["action"],
                               "agent_action": v.get("agent_action"), "n_evidence": len(ev)}
        if qt == "utility" and not v["utility_correct"]:
            stages = {}
            for pat in v.get("missed") or _list((cp.get("judge_spec") or {}).get("include")):
                if pattern_hits(ev_text, pat):
                    s = "in_evidence"
                else:
                    holders = [r for r in live if pattern_hits(r["text"], pat)]
                    current = [r for r in holders if r["superseded_seq"] is None or r["superseded_seq"] > asof]
                    if any(r["views"].get(asker) in ("full", "redacted") for r in current):
                        s = "readable_not_released"
                    elif current:
                        s = "stored_not_readable"
                    elif holders:
                        s = "superseded"
                    else:
                        s = "not_in_memory"
                stages[pat] = s
            out["pattern_stages"] = stages
            out["stage"] = max(stages.values(), key=STAGES.index) if stages else "in_evidence"
            if out["stage"] == "in_evidence":
                out["stage"] = "answerer_refused" if v["action"] != "answer" else "answerer_incomplete"
        if v["context_exposed"] or v["privacy_leak"] or v["deletion_leak"]:
            spec = cp.get("judge_spec") or {}
            pats = _list(cp.get("leak_targets")) + (_list(spec.get("not_include")) if qt == "privacy" else [])
            exp = []
            for e in ev:
                hits = [p for p in pats if pattern_hits(e["text"], p)]
                if not hits:
                    continue
                rec = next((r for r in idx["records"] if r["id"] == e["record_id"]), None)
                seq = rec["created_seq"] if rec else 0
                when = "no_deletion_in_episode"
                if qt == "safety":
                    before = [d for d in idx["delete_seqs"] if d <= asof]
                    when = ("after_deletion" if before and seq > max(before) else
                            "before_deletion_not_removed" if before else "no_deletion_before_query")
                exp.append({"record_id": e["record_id"], "speaker": e["principal_id"],
                            "turn": rec["turn"] if rec else "", "hits": hits[:3], "when": when})
            out["exposures"] = exp
            out["leak_kind"] = ("answer_leak" if (v["privacy_leak"] or v["deletion_leak"]) else "") + \
                ("+context" if v["context_exposed"] else "")
        rows.append(out)
    write_jsonl(run / "diagnostics.jsonl", rows)
    util = Counter(r["stage"] for r in rows if "stage" in r)
    pat = Counter(s for r in rows for s in (r.get("pattern_stages") or {}).values())
    exp_when = Counter(e["when"] for r in rows if r["query_type"] == "safety" for e in r.get("exposures", []))
    exp_priv = Counter(r["asker_role"] for r in rows if r["query_type"] == "privacy" and r.get("exposures"))
    summary = {
        "utility_failures_by_stage": dict(util), "missed_patterns_by_stage": dict(pat),
        "utility_failures_by_asker_role": dict(Counter(r["asker_role"] for r in rows if "stage" in r)),
        "deletion_exposures_by_timing": dict(exp_when),
        "privacy_exposures_by_asker_role": dict(exp_priv),
        "delete_ops_per_episode": {e: by_ep[e]["n_delete_ops"] for e in by_ep},
        "safety_checkpoints_per_episode": dict(Counter(c["episode_id"] for c in data_cps.values()
                                                       if c.get("query_type") == "safety" and c["episode_id"] in by_ep)),
    }
    (run / "diagnostics.json").write_text(json.dumps(summary, indent=1))
    return summary
