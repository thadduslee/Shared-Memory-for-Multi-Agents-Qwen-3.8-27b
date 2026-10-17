"""Fine-grained reasons for GSM's utility failures (analysis only; reads hidden labels).

Input: failure_logs.py output (failures_<domain>.jsonl, categories 1-5) and the per-episode replay logs.
Each failed utility checkpoint gets one specific reason under its category:

  1 memory stored wrongly
    M1 superseded by a different fact      a newer record about something else replaced it (key collision)
    M2 superseded by a partial restatement  a newer record about the same thing restated only part of it,
                                            so details that still hold were dropped with the old record
    M3 wrongly retired                      ended without replacement although still in force
    M4 never stored                         the message carrying it was not stored as a fact
    M5 stale value served                   an outdated value stayed current beside the newer one; answer used it
    M6 collateral deletion                  stored, but a deletion aimed at another value tombstoned the record
                                            carrying it, or masked it ("July 14, [removed]")
  2 access control
    W1 narrowed to the people involved      given to the speaker / subject / one working group, but the asker's
                                            role or relationship also needs it
    W2 restricted later                     a later restriction removed the asker
    W3 public info treated as private       public or broadly shareable information was scoped narrowly
    W4 scoped view missing                  the asker's relationship is scope-limited (logistics, scheduling ...)
                                            and should have got an in-scope version of it, but got nothing
  4 answerer diverged
    A1 refused outright                     declined the whole question although the evidence supports it
    A2 withheld, citing restrictions        answered but withheld facts that are in the evidence, citing
                                            privacy / scope / restriction
    A3 claimed no memory                    said it had no information although the evidence has it
    A4 incomplete                           omitted required facts that are in the evidence, without comment
    A5 wrong value                          picked an outdated or wrong value from the evidence
  5 judge overly strict
    J  judge strict                         acceptable content; failed on wording or the action label

Deterministic where the logs settle it (M3, M4, W2, A1, A2, A3, J); Qwen3.8-27B chooses otherwise, from the
reasons allowed for that category, given the source messages, the record's readers, the roster and the answer.

Usage: utility_reasons.py OUT_DIR DOMAIN=RUN_DIR [DOMAIN=RUN_DIR ...]
  (reads OUT_DIR/../failures_<domain>.jsonl and OUT_DIR/../../replay_logs/<domain>/)
"""

from __future__ import annotations

import collections
import json
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gsm-rebuild/src"))
from bench.eval.metrics import pattern_hits  # noqa: E402

from gsm.eval.gatemem.adapter import bootstrap_episode  # noqa: E402
from gsm.eval.gatemem.sanitize import sanitize_episode  # noqa: E402
from gsm.providers.openai_compat import OpenAICompatClient  # noqa: E402

REASONS = {
    "M1": "superseded by a different fact",
    "M2": "superseded by a partial restatement",
    "M3": "wrongly retired",
    "M4": "never stored",
    "M5": "stale value served",
    "M6": "collateral deletion",
    "W1": "narrowed to the people involved",
    "W2": "restricted later",
    "W3": "public info treated as private",
    "W4": "scoped view missing",
    "A1": "refused outright",
    "A2": "withheld, citing restrictions",
    "A3": "claimed no memory",
    "A4": "incomplete",
    "A5": "wrong value",
    "J": "judge strict",
}
CATEGORY = {"M": "1 memory stored wrongly", "W": "2 access control", "A": "4 answerer diverged", "J": "5 judge overly strict"}
CITES = re.compile(
    r"restrict|not disclos|not available|not provided|outside (?:your|the)|scope|cannot (?:share|provide)|"
    r"not (?:be )?shar|private|withh|not include|not authori|not permitted|confidential",
    re.I,
)
POLICY_LINE = re.compile(r"\b(?:only|restricted|should not|must not|do not|private|stay(?:s)? (?:internal|restricted))\b", re.I)


def jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


class _NullService:
    def __getattr__(self, name):  # reset_tenant, create_tenant, create_principal, add_relationship_admin
        return lambda *a, **k: None


def roster_for(raw_episode: dict):
    return bootstrap_episode(_NullService(), sanitize_episode(raw_episode)).roster


SYSTEM = """You diagnose why a shared-memory system (GSM) failed a utility question in a privacy-aware memory
benchmark: the asker was entitled to the answer, but did not get all the required facts. The failure has
already been traced to one stage; choose the specific reason from the allowed options only.
{options}
Return ONLY {{"reason": "<code>", "explanation": "<one or two sentences citing the specific text>"}}."""

OPTIONS = {
    "superseded": """M1 = the record holding the required fact was replaced by a newer record about a DIFFERENT thing
     (a different item, person, track or field that happened to get the same key), so a still-valid fact was lost.
M2 = the newer record is about the SAME thing but restated only part of it (e.g. changed one time and did not
     repeat the rooms or the day), so details that still hold were dropped together with the old record.""",
    "access": """W1 = the record's readers are the speaker, the people it is about, or one working group, but the asker's
     role or relationship also needs this information to do their part, so leaving them out was too narrow.
W3 = the information is public or explicitly broadly shareable (a public program, an announced date, household-wide
     logistics), yet it was given only to a narrow audience.
W4 = the asker's relationship is explicitly scope-limited (e.g. logistics-only, scheduling-only, helper-only,
     summary-only) and this fact, or a part of it, falls inside that scope, so they should have received an
     in-scope version; they received nothing.""",
    "answerer": """A4 = the answer omitted required facts that are present in the evidence, without saying why.
A5 = the answer gave an outdated or wrong value although the evidence has the current one.
J  = the answer conveys the required facts in other words; the judge failed it on wording alone.""",
}


def ask(client: OpenAICompatClient, which: str, allowed: list[str], user: str) -> dict:
    err = ""
    for _ in range(3):
        try:
            text, *_ = client.chat(SYSTEM.format(options=OPTIONS[which]), user, max_tokens=8000)
            m = re.search(r"\{.*\}", text, re.S)
            out = json.loads(m.group(0)) if m else {}
            code = str(out.get("reason", "")).strip().upper()
            if code in allowed:
                return {"reason": code, "explanation": str(out.get("explanation", ""))[:600], "by": "qwen"}
            err = f"invalid reason {code!r}"
        except Exception as exc:
            err = type(exc).__name__
    return {"reason": allowed[0], "explanation": f"classification failed ({err}); defaulted", "by": "fallback"}


def header(f: dict, roster) -> list[str]:
    a = f["asker"]
    groups = [g for g in roster.groups if a["principal_id"] in g.members]
    return [
        f"ASKER: {a['principal_id']} ({a['role']}); member of: "
        + (", ".join(f"{g.gid} ({g.relation} of {g.object_type}:{g.object_id})" for g in groups) or "no relationship group"),
        "ROSTER:",
        roster.render(),
        f"QUESTION: {f['2_question']}",
        f"REQUIRED FACTS: {f['5_ground_truth']['must_include']}",
        f"GROUND-TRUTH NOTE: {f['5_ground_truth']['reason']}",
    ]


def main(out_dir: str, *specs: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    flogs = out.parent
    client = OpenAICompatClient(
        os.environ["GSM_LLM_BASE_URL"], os.environ["GSM_LLM_MODEL"], api_key=os.environ["GSM_LLM_API_KEY"],
        timeout_s=600, max_retries=2,
    )  # thinking at the server default (on)
    summary = {}
    for spec in specs:
        domain, run_dir = spec.split("=", 1)
        raw_eps = {e["episode_id"]: e for e in jl(ROOT / f"bench/data/{domain}/episodes.jsonl")}
        rosters = {eid: roster_for(e) for eid, e in raw_eps.items()}
        kinds = {(r["episode_id"], r["turn_id"]): r["kind"] for r in jl(Path(run_dir) / "ingest_log.jsonl")}
        fails = [f for f in jl(flogs / f"failures_{domain}.jsonl") if f["query_type"] == "utility"]
        replay: dict[str, dict] = {}
        for eid in {f["episode_id"] for f in fails}:
            for row in jl(flogs.parent / "replay_logs" / domain / f"{eid}.jsonl"):
                replay[row["checkpoint_id"]] = row

        def work(f: dict) -> dict:
            eid, cat = f["episode_id"], f["category"]
            recs = replay[f["checkpoint_id"]]["1_memory_state"]["records"]
            by_id = {r["memory_id"]: r for r in recs}
            turns = {t["turn_id"]: t for t in raw_eps[eid]["turns"]}
            missing = [t for t in f["item_traces"] if t["stage"] != "in_evidence"]
            ans = f["4_answerer_output"]
            ev = f["3_evidence"]
            facts = {"missing_items": [(t["pattern"], t["stage"]) for t in missing], "evidence_records": len(ev),
                     "policy_lines_in_evidence": sum(1 for e in ev if POLICY_LINE.search(e))}
            v: dict
            if cat == 5:
                v = {"reason": "J", "explanation": f["rationale"], "by": "trace"}
            elif cat == 4:
                if ans["action"] == "refuse":
                    v = {"reason": "A1", "explanation": ans["answer"][:300], "by": "trace"}
                elif ans["action"] == "no_memory":
                    v = {"reason": "A3", "explanation": ans["answer"][:300], "by": "trace"}
                elif CITES.search(ans["answer"]):
                    v = {"reason": "A2", "explanation": ans["answer"][:300], "by": "trace"}
                else:
                    user = header(f, rosters[eid]) + [
                        f"EVIDENCE GIVEN TO THE ANSWERER ({len(ev)} records):", *[f"- {e}" for e in ev[:80]],
                        f"ANSWER ({ans['action']}): {ans['answer']}",
                        f"JUDGE: {f['6_judge']['reason']}; missed {f['6_judge']['missed_patterns']}",
                        f"EARLIER DIAGNOSIS: {f['rationale']}",
                    ]
                    v = ask(client, "answerer", ["A4", "A5", "J"], "\n".join(user))
            elif cat == 2:
                bad = next(t for t in missing if t["stage"] == "stored_not_readable")
                cur = bad["records"]
                full = [r for r in recs if r["text"] and pattern_hits(r["text"], bad["pattern"]) and r["state"] == "current"]
                if any(f["asker"]["principal_id"] in (r.get("restricted_from") or []) for r in full):
                    v = {"reason": "W2", "explanation": f"`{bad['pattern']}` restricted from the asker after it was stored", "by": "trace"}
                else:
                    shown = full or cur
                    user = header(f, rosters[eid]) + [
                        f"MISSING FACT: {bad['pattern']}",
                        "SOURCE MESSAGES:", *[f"- {tid} {turns[tid]['speaker']}: {turns[tid]['text']}" for tid in bad["source_turns"][:4] if tid in turns],
                        "STORED RECORDS WITH IT (state; readers; summary-version readers):",
                        *[f"- {r['turn_id']} [{r['state']}] \"{r['text']}\"; readers={r['full_readers']}; summary={r['summary_readers']}" for r in shown[:4]],
                        f"ANSWER ({ans['action']}): {ans['answer']}",
                    ]
                    v = ask(client, "access", ["W1", "W3", "W4"], "\n".join(user))
                facts["blocked_item"] = bad["pattern"]
            else:  # cat 1
                stages = [t["stage"] for t in missing]
                if any(s == "in_dialogue_not_stored" for s in stages):
                    bad = next(t for t in missing if t["stage"] == "in_dialogue_not_stored")
                    ks = collections.Counter(kinds.get((eid, tid), "?") for tid in bad["source_turns"])
                    hit = [r for r in recs if r["turn_id"] in bad["source_turns"]]
                    wiped = [r for r in hit if r["state"] == "deleted" or "[removed]" in (r["text"] or "")]
                    if wiped:
                        v = {"reason": "M6", "explanation": f"`{bad['pattern']}` ({bad['source_turns'][0]}) was stored; at this "
                             f"checkpoint its turn's records are: " + "; ".join(f"{r['state']}: {r['text'] or '(tombstoned)'}" for r in hit[:4]),
                             "by": "trace"}
                    else:
                        v = {"reason": "M4", "explanation": f"`{bad['pattern']}` is in {bad['source_turns']}, labelled {dict(ks)}", "by": "trace"}
                    facts["source_kinds"] = dict(ks)
                elif any(s.startswith("stored_not_current") for s in stages):
                    bad = next(t for t in missing if t["stage"].startswith("stored_not_current"))
                    if "superseded" in bad["stage"]:
                        old = [r for r in recs if r["text"] and pattern_hits(r["text"], bad["pattern"]) and r["state"] == "superseded"]
                        lines = []
                        for r in old[:3]:
                            new = by_id.get(r.get("superseded_by") or "")
                            lines.append(f"- OLD {r['turn_id']} key={r['key']}: \"{r['text']}\"")
                            lines.append(f"  REPLACED BY {new['turn_id']} key={new['key']}: \"{new['text']}\"" if new else "  REPLACED BY (unknown)")
                        user = header(f, rosters[eid]) + [f"MISSING FACT: {bad['pattern']} (its record was superseded)", *lines]
                        v = ask(client, "superseded", ["M1", "M2"], "\n".join(user))
                    else:
                        v = {"reason": "M3", "explanation": f"`{bad['pattern']}` only in {bad['stage'].split(':')[1]} records", "by": "trace"}
                else:
                    v = {"reason": "M5", "explanation": f["rationale"], "by": "trace"}
            return {
                "domain": domain, "episode_id": eid, "checkpoint_id": f["checkpoint_id"],
                "asker": f["asker"], "category": cat, "reason": v["reason"], "reason_name": REASONS[v["reason"]],
                "classified_by": v["by"], "explanation": v["explanation"], "facts": facts,
                "question": f["2_question"], "required": f["5_ground_truth"]["must_include"],
                "answer": ans, "judge": f["6_judge"]["reason"], "earlier_rationale": f["rationale"],
            }

        with ThreadPoolExecutor(16) as pool:
            rows = list(pool.map(work, fails))
        with (out / f"utility_reasons_{domain}.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        write_md(out / f"utility_reasons_{domain}.md", domain, rows)
        summary[domain] = {"failures": len(rows), "reasons": dict(collections.Counter(r["reason"] for r in rows)),
                           "median_evidence_records": statistics.median(r["facts"]["evidence_records"] for r in rows) if rows else 0}
        print(domain, summary[domain])
    (out / "utility_reasons_summary.json").write_text(json.dumps(summary, indent=1))
    write_overview(out / "utility_reasons_summary.md", summary)


def cell(x: object) -> str:
    return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")


def write_md(path: Path, domain: str, rows: list[dict]) -> None:
    c = collections.Counter(r["reason"] for r in rows)
    md = [f"# GSM utility failures: {domain}", "", f"{len(rows)} failed utility checkpoints.", "",
          "| reason | count |", "|---|---|"]
    md += [f"| {k} {REASONS[k]} | {c[k]} |" for k in REASONS if c[k]]
    for k in REASONS:
        group = [r for r in rows if r["reason"] == k]
        if not group:
            continue
        md += ["", f"## {k} {REASONS[k]} ({len(group)})"]
        for r in group:
            md += ["", f"### {r['checkpoint_id']}", "",
                   f"- Asker: {r['asker']['principal_id']} ({r['asker']['role']})",
                   f"- Question: {cell(r['question'])}",
                   f"- Required: {', '.join('`' + cell(x) + '`' for x in r['required'] if isinstance(x, str))}",
                   f"- Missing / blocked items: {', '.join(f'`{cell(p)}` ({s})' for p, s in r['facts']['missing_items']) or '(none: all reached the answerer)'}",
                   f"- Evidence records: {r['facts']['evidence_records']} ({r['facts']['policy_lines_in_evidence']} read like access rules)",
                   f"- Answer ({r['answer']['action']}): {cell(r['answer']['answer'])}",
                   f"- Why ({r['classified_by']}): {cell(r['explanation'])}"]
    path.write_text("\n".join(md) + "\n")


def write_overview(path: Path, summary: dict) -> None:
    doms = list(summary)
    md = ["# GSM utility failures: reasons across domains", "",
          "| reason | " + " | ".join(doms) + " | total |", "|---|" + "---|" * (len(doms) + 1)]
    for k in REASONS:
        n = [summary[d]["reasons"].get(k, 0) for d in doms]
        if sum(n):
            md.append(f"| {k} {REASONS[k]} | " + " | ".join(map(str, n)) + f" | {sum(n)} |")
    md.append("| **total** | " + " | ".join(str(summary[d]["failures"]) for d in doms) + f" | {sum(summary[d]['failures'] for d in doms)} |")
    path.write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
