"""attribution.py on a handcrafted trace: one checkpoint per rule branch."""

from __future__ import annotations

from contracts import Verdict
from loop.attribution import build_attribution


def _ev(seq, component, op, outcome="applied", record_ids=(), op_args=None, checkpoint_id="", denied=(),
        llm=False, reason="", signal=""):
    return {"seq": seq, "component": component, "turn_id": f"t{seq:03d}", "op": op, "outcome": outcome,
            "record_ids": list(record_ids), "op_args": op_args or {}, "checkpoint_id": checkpoint_id,
            "denied_ids": list(denied), "llm_called": llm, "reason": reason, "signal_kind": signal,
            "chunk_id": "", "episode_id": "E"}


def _q(seq, cid, released=(), denied=(), outcome="applied"):
    return _ev(seq, "retriever", "query", outcome, released, {"action": "x"}, cid, denied)


def _v(cid, qt, *, util=None, priv=None, dele=None):
    return Verdict(checkpoint_id=cid, query_type=qt, expected_action="answer", action="answer",
                   action_correct=True, utility_correct=util, privacy_leak=priv, deletion_leak=dele,
                   over_refusal=False)


def _pred(cid, asker="p_a", role="role_a"):
    return {"checkpoint_id": cid, "episode_id": "E", "asker": {"principal_id": asker, "role": role},
            "output": {"action": "answer", "debug": {"evidence": []}}}


TRACE = [
    _ev(1, "processor", "add", record_ids=["r1"], op_args={"audience": ["p_b"]}),
    _ev(2, "processor", "add", record_ids=["r2"], op_args={"audience": ["p_b"]}),
    _ev(3, "processor", "revoke", record_ids=["r2"], op_args={"record_ids": ["r2"], "principal_ids": ["p_a"]},
        llm=True, signal="instruction"),
    _q(4, "u_denied_policy", denied=["r2"]),            # utility, denied, last policy op = revoke
    _q(5, "u_denied_default", denied=["r1"]),           # utility, denied, never granted -> creating add
    _q(6, "u_nothing"),                                 # utility, nothing at all -> retriever search
    _ev(7, "processor", "add", record_ids=["r3"], op_args={"audience": ["p_a"]}),
    _q(8, "u_released", released=["r3"]),               # utility, released -> retriever select
    _ev(9, "front_door", "add", op_args={"suggested_audience": ["p_a", "p_c"]}, record_ids=["r4"]),
    _q(10, "p_frontdoor", released=["r4"]),             # privacy, widened at the front door
    _ev(11, "processor", "grant", record_ids=["r1"],
        op_args={"record_ids": ["r1"], "principal_ids": ["p_c"], "authority": "p_b"}, llm=True),
    _q(12, "p_grant", released=["r1"]),                 # privacy (asker p_c), granted by processor
    _ev(13, "processor", "delete", record_ids=["r5"], op_args={"record_ids": ["r5"]}),
    _ev(14, "processor", "derive", record_ids=["r6"], op_args={"source_record_ids": ["r5"], "text": "x"}),
    _q(15, "s_derived", released=["r6"]),               # deletion: derived copy of a deleted source
    _ev(16, "processor", "delete", outcome="rejected", record_ids=["r7"], op_args={"record_ids": ["r7"]},
        llm=True, reason="delete: unknown record"),
    _q(17, "s_rejected", released=["r7"]),              # deletion: the delete was rejected
    _ev(18, "processor", "add", record_ids=["r8"]),
    _q(19, "s_never", released=["r8"]),                 # deletion: no delete ever emitted
    _ev(20, "processor", "add", outcome="fallback", record_ids=["r9"]),
    _q(21, "u_fallback", denied=["r9"]),                # a fallback in the chain wins
    _q(22, "p_structural"),                             # privacy leak with nothing released
    _ev(23, "processor", "grant", outcome="rejected", llm=True, reason="grant missing authority"),
    # the real front door: no record ids, linked to the record by chunk_id
    {**_ev(24, "front_door", "chunk:fact", op_args={"audience": ["p_a", "p_d"]}), "chunk_id": "c24"},
    {**_ev(25, "processor", "add", record_ids=["r10"], op_args={"chunk_id": "c24"}), "chunk_id": "c24"},
    _q(26, "p_chunk_linked", released=["r10"]),
    # a retriever fallback (nothing to select) does not explain a denial upstream
    _q(27, "u_query_fallback", denied=["r2"], outcome="fallback"),
]

VERDICTS = [
    _v("u_denied_policy", "utility", util=False), _v("u_denied_default", "utility", util=False),
    _v("u_nothing", "utility", util=False), _v("u_released", "utility", util=False),
    _v("p_frontdoor", "privacy", priv=True), _v("p_grant", "privacy", priv=True),
    _v("s_derived", "safety", dele=True), _v("s_rejected", "safety", dele=True),
    _v("s_never", "safety", dele=True), _v("u_fallback", "utility", util=False),
    _v("p_structural", "privacy", priv=True),
    _v("p_chunk_linked", "privacy", priv=True), _v("u_query_fallback", "utility", util=False),
    _v("u_ok", "utility", util=True),
]

PREDS = [_pred(v.checkpoint_id) for v in VERDICTS]
PREDS[5] = _pred("p_grant", asker="p_c", role="role_c")


def test_each_rule_branch():
    failures, report = build_attribution(VERDICTS, PREDS, TRACE)
    got = {f.checkpoint_id: (f.attributed_component, f.attributed_op) for f in failures}
    assert got == {
        "u_denied_policy": ("processor", "revoke"),
        "u_denied_default": ("processor", "add"),
        "u_nothing": ("retriever", "search"),
        "u_released": ("retriever", "select"),
        "p_frontdoor": ("front_door", "add"),
        "p_grant": ("processor", "grant"),
        "s_derived": ("librarian", "derive"),
        "s_rejected": ("processor", "delete"),
        "s_never": ("processor", "delete"),
        "u_fallback": ("processor", "add"),
        "p_structural": ("retriever", "select"),
        "p_chunk_linked": ("front_door", "chunk:fact"),
        "u_query_fallback": ("processor", "revoke"),
    }
    by_id = {f.checkpoint_id: f for f in failures}
    assert "fallback" in by_id["u_fallback"].note
    assert "rejected" in by_id["s_rejected"].note
    assert "no delete" in by_id["s_never"].note
    # the chain holds the decisions on the involved records, in order, ending with the query
    chain = by_id["u_denied_policy"].chain
    assert [e["seq"] for e in chain] == [2, 3, 4] and chain[-1]["checkpoint_id"] == "u_denied_policy"
    assert by_id["u_denied_policy"].denied == ["r2"] and by_id["u_denied_policy"].released == []
    assert [e["seq"] for e in by_id["p_chunk_linked"].chain] == [24, 25, 26]
    assert report["n_failures"] == 13 and "u_ok" not in by_id


def test_report_tables():
    _, report = build_attribution(VERDICTS, PREDS, TRACE)
    cop = report["by_component_op_outcome"]
    assert cop["retriever|select|applied"] == 2
    assert cop["processor|add|fallback"] == 1
    assert cop["processor|delete|rejected"] == 1
    assert report["by_component"]["processor"] == 7
    assert report["by_signal_kind"] == {"instruction": 2}
    rej = report["prompt_rejections"]["processor"]
    assert rej["rejected"] == 2 and rej["llm_decisions"] == 4 and rej["rejection_rate"] == 0.5
    assert rej["top_reasons"][0]["n"] == 1
    roles = report["by_requester_role"]
    row = next(r for r in roles["by_group"] if r["group"] == "role_a")
    assert row["n_utility"] == 7 and row["n_utility_failed"] == 6
    assert row["utility_nothing_released"] == 5 and row["utility_denied_only"] == 4
    assert roles["saturated_groups"] == []       # 6/7 < 0.95


def test_saturation_flags_a_closed_group():
    verdicts = [_v(f"c{i}", "utility", util=False) for i in range(4)]
    preds = [_pred(f"c{i}", role="role_x") for i in range(4)]
    trace = [_ev(1, "processor", "add", record_ids=["r1"])] + [_q(2 + i, f"c{i}", denied=["r1"]) for i in range(4)]
    _, report = build_attribution(verdicts, preds, trace)
    roles = report["by_requester_role"]
    assert roles["saturated_groups"] == ["role_x"] and roles["gate_closed_for_every_group"]
