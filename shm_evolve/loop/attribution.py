"""Join each failed verdict to the trace decisions that produced it.

WHY. A score says THAT the memory failed; the reflector has to change ONE
component's prompt, so it needs to know WHICH decision failed. Every component
writes a TraceEvent per decision (DESIGN.md §2.7) with the record ids it
touched; the retriever writes one per checkpoint with the released and denied
ids. Joining a failed checkpoint to the events that created or changed those
records is the chain; one event in it is the DECISIVE one, and its component
and op are the attribution.

THE RULE (applied in this order; "touching" = the event's record ids, or a
record-id argument in op_args, intersect the set in question; a front-door
event touches the records created from its chunk, linked by `chunk_id`,
because the front door decides audiences before any record exists):

  0. FALLBACK FIRST. If any decision in the chain that touched the records ran
     a deterministic fallback (outcome == "fallback": the model was
     unavailable or over budget), the latest one is decisive. A missing grant
     downstream of a processor that never got to read the turn is the
     fallback's failure, not the prompt's. The retriever's own query event is
     NOT in this rule: it falls back whenever there is nothing to select, and
     that cannot explain a denial upstream. When the rules below land on the
     retriever and its query event fell back, the outcome says so.
  1. UTILITY, nothing released, something denied -> the gate refused what the
     asker needed. Decisive: the LAST POLICY op (grant/revoke/restrict/expire)
     touching the denied records -- usually a revoke/restrict that was too
     broad. If no policy op ever touched them, the records stayed at the
     default-private audience: decisive is the op that CREATED them (no grant
     was ever emitted).
  2. UTILITY, nothing released, nothing denied -> retriever/"search": no
     candidate at all, so either the search missed or the content was never
     stored (the chain is empty either way; the note says which is likelier).
  3. UTILITY, something released -> retriever/"select": the right records were
     not among those chosen, or not enough of them.
  4. PRIVACY leak, something released -> the op that GRANTED a leaked record to
     the asker: the latest `grant` naming the asker, else the latest event whose
     op_args put the asker in `suggested_audience`/`audience` (front door or an
     add). If none, the asker already held the key by default (they spoke it,
     or it was about them): retriever/"select" released it to the wrong
     question.
  5. DELETION leak, something released -> a derived/merged copy whose source
     was deleted -> the librarian (tombstone without shredding the derivation);
     else a delete/shred touching the record that was REJECTED -> its component;
     else -> processor/"delete" (no delete was ever emitted for it).
  6. PRIVACY or DELETION leak with nothing released -> retriever/"select":
     structural (an answering action with no evidence), i.e. the action choice.

"search" and "select" are not ops in ops.py; they name the two model-driven
steps of the retriever's one "query" event (DESIGN.md §2.5).

Beyond per-failure rows, `attribution.json` carries:
  * counts by (component, op, outcome), component, query type, signal_kind;
  * the DENIAL-SATURATION table from the old judge: utility failure rate per
    requester role, a group SATURATED when it fails ~always with nothing
    released (a structural policy hole, not a tuning problem), and whether the
    gate is closed for every group. No margin over the corpus rate: a run
    whose policy is empty fails every group at 1.0 and that is the finding;
  * `prompt_rejections`: per component, how often its model reply was
    REJECTED by the scaffold (outcome rejected with llm_called) -- direct
    evidence the prompt's output contract is being violated, independent of
    any score.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from contracts import AttributedFailure, Component, Outcome, Verdict
from loop.jsonx import write_json, write_jsonl

POLICY_OPS = frozenset({"grant", "revoke", "restrict", "expire"})
CREATE_OPS = frozenset({"add", "split", "merge", "supersede", "derive"})
DERIVE_OPS = frozenset({"derive", "merge"})
DELETE_OPS = frozenset({"delete", "shred"})
_ID_ARGS = ("record_ids", "source_record_ids", "old_record_id", "record_id", "other_record_id", "new_record_id")
CHAIN_MAX = 40


def _ids_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(v) for v in value if isinstance(v, (str, int))}
    return set()


def touched(ev: dict[str, Any]) -> set[str]:
    ids = set(ev.get("record_ids") or [])
    args = ev.get("op_args") or {}
    for key in _ID_ARGS:
        ids |= _ids_in(args.get(key))
    return ids


def _sources(ev: dict[str, Any]) -> set[str]:
    args = ev.get("op_args") or {}
    return _ids_in(args.get("source_record_ids")) | _ids_in(args.get("record_ids"))


def _audience_includes(ev: dict[str, Any], principal: str) -> bool:
    args = ev.get("op_args") or {}
    for key in ("principal_ids", "suggested_audience", "audience"):
        if principal and principal in _ids_in(args.get(key)):
            return True
    return False


def _is_query(ev: dict[str, Any]) -> bool:
    return bool(ev.get("checkpoint_id")) and ev.get("component") == Component.RETRIEVER


def _toucher(events: list[dict[str, Any]]) -> Callable[[dict[str, Any]], set[str]]:
    """`touched`, extended so a front-door event touches the records its chunk
    became (the processor's creation ops carry the same chunk_id)."""
    by_chunk: dict[str, set[str]] = defaultdict(set)
    for e in events:
        if e.get("chunk_id") and e.get("op") in CREATE_OPS:
            by_chunk[str(e["chunk_id"])] |= touched(e)

    def touch(e: dict[str, Any]) -> set[str]:
        ids = touched(e)
        if e.get("component") == Component.FRONT_DOOR and e.get("chunk_id"):
            ids = ids | by_chunk.get(str(e["chunk_id"]), set())
        return ids

    return touch


def _attribute(qt: str, released: list[str], denied: list[str], chain: list[dict[str, Any]],
               query_ev: dict[str, Any] | None, asker_id: str, episode_events: list[dict[str, Any]],
               touched: Callable[[dict[str, Any]], set[str]] = touched,
               ) -> tuple[str, str, str, dict[str, Any] | None]:
    """(component, op, note, decisive event). See the module docstring."""
    rel, den = set(released), set(denied)
    body = [e for e in chain if not _is_query(e)]
    fallbacks = [e for e in body if e.get("outcome") == Outcome.FALLBACK]
    if fallbacks:
        ev = fallbacks[-1]
        return ev["component"], ev["op"], "a deterministic fallback ran (model unavailable or over budget)", ev
    retriever = Component.RETRIEVER.value

    if qt == "utility":
        if not rel and den:
            pol = [e for e in body if e.get("op") in POLICY_OPS and touched(e) & den]
            if pol:
                return pol[-1]["component"], pol[-1]["op"], "nothing released; the last policy op on the denied records", pol[-1]
            made = [e for e in body if e.get("op") in CREATE_OPS and touched(e) & den]
            if made:
                return (made[-1]["component"], made[-1]["op"],
                        "nothing released; no policy op ever widened the denied records beyond their default", made[-1])
            return retriever, "query", "nothing released; denied records have no recorded provenance", query_ev
        if not rel:
            return retriever, "search", "no candidate opened or denied: the search missed or nothing was stored", query_ev
        return retriever, "select", "records were released but the answer was not among them", query_ev

    if not rel:
        return retriever, "select", "structural: an answering action with nothing released", query_ev

    if qt == "privacy":
        grants = [e for e in body if e.get("op") == "grant" and touched(e) & rel]
        named = [e for e in grants if _audience_includes(e, asker_id)]
        if named or grants:
            ev = (named or grants)[-1]
            return ev["component"], ev["op"], "a grant put the leaked record within the asker's reach", ev
        widened = [e for e in body if touched(e) & rel and _audience_includes(e, asker_id)]
        if widened:
            ev = widened[-1]
            return ev["component"], ev["op"], "the asker was placed in the leaked record's audience here", ev
        return retriever, "select", "the asker held the key by default; the released set was wrong for the question", query_ev

    # safety
    deleted = {i for e in episode_events if e.get("op") in DELETE_OPS and e.get("outcome") == Outcome.APPLIED
               for i in touched(e)}
    derived = [e for e in body if e.get("op") in DERIVE_OPS and touched(e) & rel and _sources(e) & deleted]
    if derived:
        return (Component.LIBRARIAN.value, derived[-1]["op"],
                "a derived copy survived the deletion of its source (tombstone without shred)", derived[-1])
    rejected = [e for e in body if e.get("op") in DELETE_OPS and touched(e) & rel
                and e.get("outcome") == Outcome.REJECTED]
    if rejected:
        ev = rejected[-1]
        return ev["component"], ev["op"], f"a {ev['op']} on the leaked record was rejected: {ev.get('reason', '')}"[:200], ev
    applied = [e for e in body if e.get("op") in DELETE_OPS and touched(e) & rel]
    if applied:
        return Component.LIBRARIAN.value, applied[-1]["op"], "tombstoned yet still released", applied[-1]
    return Component.PROCESSOR.value, "delete", "no delete was ever emitted for the released record(s)", None


def attribute_failures(
    verdicts: list[Verdict], predictions: list[dict[str, Any]], traces: list[dict[str, Any]],
) -> list[AttributedFailure]:
    preds = {p["checkpoint_id"]: p for p in predictions}
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in traces:
        by_episode[str(ev.get("episode_id") or "")].append(ev)
    for evs in by_episode.values():
        evs.sort(key=lambda e: int(e.get("seq") or 0))

    touchers: dict[str, Callable[[dict[str, Any]], set[str]]] = {}
    out: list[AttributedFailure] = []
    for v in verdicts:
        if not v.failed:
            continue
        pred = preds.get(v.checkpoint_id) or {}
        eid = str(pred.get("episode_id") or "")
        asker = pred.get("asker") or {}
        events = by_episode.get(eid, [])
        if eid not in touchers:
            touchers[eid] = _toucher(events)
        touch = touchers[eid]
        qevs = [e for e in events if e.get("checkpoint_id") == v.checkpoint_id]
        # The retriever also logs read-time signals under the same checkpoint
        # id, AFTER its query event and with empty record_ids. Taking the last
        # event blindly read every utility failure as "nothing released" on the
        # first live run (stage_a_office); the query event is the one that
        # was not merely logged.
        acted = [e for e in qevs if e.get("outcome") != "logged"]
        query_ev = (acted or qevs)[-1] if qevs else None
        if query_ev is not None:
            released = [str(r) for r in query_ev.get("record_ids") or []]
            denied = [str(r) for r in query_ev.get("denied_ids") or []]
            horizon = int(query_ev.get("seq") or 0)
        else:
            ev_list = ((pred.get("output") or {}).get("debug") or {}).get("evidence") or []
            released = [str(e.get("record_id")) for e in ev_list if isinstance(e, dict) and e.get("record_id")]
            denied = []
            horizon = 1 << 62
        relevant = set(released) | set(denied)
        chain = [e for e in events if not _is_query(e) and int(e.get("seq") or 0) <= horizon
                 and touch(e) & relevant]
        chain = chain[-CHAIN_MAX:] + ([query_ev] if query_ev is not None else [])
        comp, op, note, _ = _attribute(v.query_type, released, denied, chain, query_ev,
                                       str(asker.get("principal_id") or ""),
                                       [e for e in events if int(e.get("seq") or 0) <= horizon], touch)
        out.append(AttributedFailure(
            checkpoint_id=v.checkpoint_id, query_type=v.query_type, asker_role=str(asker.get("role") or ""),
            verdict=asdict(v), released=released, denied=denied,
            chain=[{k: val for k, val in e.items() if k != "episode_id"} for e in chain],
            attributed_component=comp, attributed_op=op, note=note,
        ))
    return out


def decisive_outcome(f: AttributedFailure) -> str:
    for ev in reversed(f.chain):
        if ev.get("component") == f.attributed_component and ev.get("op") == f.attributed_op:
            return str(ev.get("outcome") or "")
    if f.attributed_component == Component.RETRIEVER and f.chain and _is_query(f.chain[-1]):
        return str(f.chain[-1].get("outcome") or "")
    return "none"


def denial_saturation(verdicts: list[Verdict], predictions: list[dict[str, Any]],
                      failures: list[AttributedFailure]) -> dict[str, Any]:
    """Utility failure rate per requester role; SATURATED groups fail ~always
    with nothing released. Grouped on `asker.role`, the one categorical field
    every domain's checkpoints carry (it is visible data, not an annotation)."""
    preds = {p["checkpoint_id"]: p for p in predictions}
    fail_by_id = {f.checkpoint_id: f for f in failures}
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for v in verdicts:
        role = str(((preds.get(v.checkpoint_id) or {}).get("asker") or {}).get("role") or "unknown")
        g = groups[role]
        g["n"] += 1
        g[f"n_{v.query_type}"] += 1
        if v.failed:
            g["n_failed"] += 1
            g[f"failed_{v.query_type}"] += 1
        if v.query_type != "utility" or not v.failed:
            continue
        f = fail_by_id.get(v.checkpoint_id)
        if f is not None and not f.released:
            g["utility_nothing_released"] += 1
            if f.denied:
                g["utility_denied_only"] += 1
    rows = []
    n_u = sum(g["n_utility"] for g in groups.values())
    n_uf = sum(g["failed_utility"] for g in groups.values())
    for role, g in groups.items():
        nu, fu = g["n_utility"], g["failed_utility"]
        rate = fu / nu if nu else 0.0
        rows.append({
            "group": role, "n": g["n"], "n_failed": g["n_failed"], "n_utility": nu, "n_utility_failed": fu,
            "utility_failure_rate": round(rate, 4), "n_privacy_failed": g["failed_privacy"],
            "n_safety_failed": g["failed_safety"], "utility_nothing_released": g["utility_nothing_released"],
            "utility_denied_only": g["utility_denied_only"],
            "saturated": bool(nu >= 3 and rate >= 0.95 and g["utility_nothing_released"] >= 0.5 * fu),
        })
    rows.sort(key=lambda r: (not r["saturated"], -r["utility_failure_rate"], -r["n_utility"], r["group"]))
    with_util = [r for r in rows if r["n_utility"]]
    return {
        "corpus_utility_failure_rate": round(n_uf / n_u, 4) if n_u else 0.0,
        "by_group": rows,
        "saturated_groups": [r["group"] for r in rows if r["saturated"]],
        # Tested on the condition, not on `saturated` (which needs n>=3): a
        # domain with one question per role would otherwise never report it.
        "gate_closed_for_every_group": bool(with_util) and all(
            r["utility_failure_rate"] >= 0.95
            and r["utility_nothing_released"] >= 0.5 * max(r["n_utility_failed"], 1) for r in with_util),
    }


def prompt_rejections(traces: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for comp in Component:
        evs = [e for e in traces if e.get("component") == comp.value and e.get("llm_called")]
        rej = [e for e in evs if e.get("outcome") == Outcome.REJECTED]
        reasons = Counter(str(e.get("reason") or "")[:100] for e in rej)
        out[comp.value] = {
            "llm_decisions": len(evs), "rejected": len(rej),
            "rejection_rate": round(len(rej) / len(evs), 4) if evs else 0.0,
            "top_reasons": [{"reason": r, "n": n} for r, n in reasons.most_common(5)],
        }
    return out


def build_attribution(verdicts: list[Verdict], predictions: list[dict[str, Any]],
                      traces: list[dict[str, Any]]) -> tuple[list[AttributedFailure], dict[str, Any]]:
    failures = attribute_failures(verdicts, predictions, traces)
    by_cop: Counter[str] = Counter()
    by_comp: Counter[str] = Counter()
    by_qt: Counter[str] = Counter()
    by_signal: Counter[str] = Counter()
    for f in failures:
        by_cop[f"{f.attributed_component}|{f.attributed_op}|{decisive_outcome(f)}"] += 1
        by_comp[f.attributed_component] += 1
        by_qt[f.query_type] += 1
        for kind in {str(e.get("signal_kind") or "") for e in f.chain} - {""}:
            by_signal[kind] += 1
    totals: Counter[str] = Counter(f"{e.get('component')}|{e.get('op')}|{e.get('outcome')}" for e in traces)
    signals: Counter[str] = Counter(str(e.get("signal_kind")) for e in traces if e.get("signal_kind"))
    report = {
        "n_verdicts": len(verdicts),
        "n_failures": len(failures),
        "by_query_type": dict(by_qt),
        "by_component": dict(by_comp.most_common()),
        "by_component_op_outcome": dict(by_cop.most_common()),
        "by_signal_kind": dict(by_signal.most_common()),
        "by_requester_role": denial_saturation(verdicts, predictions, failures),
        "prompt_rejections": prompt_rejections(traces),
        "trace_totals": dict(totals.most_common()),
        "trace_signal_kinds": dict(signals.most_common()),
    }
    return failures, report


def write_attribution(out_dir: Path, verdicts: list[Verdict], predictions: list[dict[str, Any]],
                      traces: list[dict[str, Any]]) -> tuple[list[AttributedFailure], dict[str, Any]]:
    failures, report = build_attribution(verdicts, predictions, traces)
    write_jsonl(Path(out_dir) / "failures.jsonl", [asdict(f) for f in failures])
    write_json(Path(out_dir) / "attribution.json", report)
    return failures, report
