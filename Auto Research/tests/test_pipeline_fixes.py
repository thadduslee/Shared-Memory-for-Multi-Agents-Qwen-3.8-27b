"""The four pipeline fixes from the rerun2 post-mortem, each pinned by a test.

1. MEMORY -- the Architect's notebook gets its own call when the design reply
   drops it (it dropped it on 0 of 39 rounds).
2. DEVELOPER TURN ECONOMY -- every fenced tool call in a turn runs, whole files
   come back in one read, and a redundant re-read is labelled.
3. ARCHITECT -- `targets_metric` reduces to a comparable key, and three designs
   on one term that did not pay are called out.
4. GATES -- the seed's contract tests decide `tests_ok`; the smoke test is a
   mandatory gate; iteration 0 measures the seed (see test_graph_paths).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from harness.dsh_client import extract_json_blocks
from nodes import architect as arch
from nodes import developer as dev
from nodes.dev_tools import DevToolbox

# ----------------------------------------------------------------------
# 2. every call in a turn runs
# ----------------------------------------------------------------------

def test_every_fenced_call_in_a_turn_is_kept() -> None:
    text = (
        "I'll read both files.\n"
        '```json\n{"tool": "read_file", "args": {"path": "memory_system/agent.py"}}\n```\n'
        '```json\n{"tool": "read_file", "args": {"path": "memory_system/store.py"}}\n```\n'
        '```json\n{"tool": "read_file", "args": {"path": "memory_system/store.py"}}\n```\n'
    )
    actions = dev._parse_actions(text)
    assert [a["args"]["path"] for a in actions] == [
        "memory_system/agent.py", "memory_system/store.py",
    ], "both distinct calls run, in order; the exact duplicate collapses"


def test_a_single_call_still_parses_the_old_way() -> None:
    text = '```json\n{"tool": "compile_check", "args": {}}\n```'
    assert [a["tool"] for a in dev._parse_actions(text)] == ["compile_check"]
    assert dev._parse_actions("no calls here at all") == []


def test_extract_json_blocks_takes_only_fenced_objects() -> None:
    text = 'prose {"tool": "x"} prose\n```json\n{"a": 1}\n```\n```\n{"b": 2}\n```'
    assert extract_json_blocks(text) == [{"a": 1}, {"b": 2}]


# ----------------------------------------------------------------------
# 2. whole files, and a labelled re-read
# ----------------------------------------------------------------------

def _read(box: DevToolbox, path: str, **args):
    return asyncio.run(box._t_read_file({"path": path, **args}))


def test_a_source_file_comes_back_in_one_read(tmp_path: Path) -> None:
    box = DevToolbox(workspace=tmp_path)
    (tmp_path / "store.py").write_text("".join(f"line_{i}\n" for i in range(1, 1001)))
    result = _read(box, "store.py")
    assert result.data["complete"] is True
    assert "line_1000\n" in result.stdout
    assert "NOT THE WHOLE FILE" not in result.stdout


def test_a_repeat_read_of_an_unchanged_file_says_so(tmp_path: Path) -> None:
    box = DevToolbox(workspace=tmp_path)
    (tmp_path / "agent.py").write_text("x = 1\n")
    first = _read(box, "agent.py")
    second = _read(box, "agent.py")
    assert "UNCHANGED" not in first.stdout
    assert "UNCHANGED since you last read it" in second.stdout
    assert "x = 1" in second.stdout, "labelled, not withheld"
    (tmp_path / "agent.py").write_text("x = 2\n")
    third = _read(box, "agent.py")
    assert "UNCHANGED" not in third.stdout, "an edit clears the label"


# ----------------------------------------------------------------------
# 4. the seed's contract tests decide tests_ok
# ----------------------------------------------------------------------

def _seed_and_workspace(tmp_path: Path, monkeypatch) -> Path:
    seed = tmp_path / "seed"
    (seed / "tests").mkdir(parents=True)
    (seed / "tests" / "test_contract.py").write_text("def test_contract():\n    assert True\n")
    monkeypatch.setattr(config, "TEMPLATES_DIR", seed)
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_contract.py").write_text("def test_contract():\n    assert True\n")
    (ws / "tests" / "test_mine.py").write_text("def test_mine():\n    assert False\n")
    return ws


def test_a_failing_self_authored_test_is_advisory(tmp_path: Path, monkeypatch) -> None:
    ws = _seed_and_workspace(tmp_path, monkeypatch)
    result = asyncio.run(DevToolbox(workspace=ws)._t_run_tests({"path": "tests"}))
    assert result.ok is False, "the suite as a whole still reports the failure"
    assert result.data["contract_ok"] is True, "but the seed's tests passed, so tests_ok will"
    assert "advisory" in result.stdout


def test_deleting_a_contract_test_does_not_pass_the_gate(tmp_path: Path, monkeypatch) -> None:
    ws = _seed_and_workspace(tmp_path, monkeypatch)
    box = DevToolbox(workspace=ws)  # the baseline is taken here, WITH the contract file
    (ws / "tests" / "test_contract.py").unlink()
    (ws / "tests" / "test_mine.py").write_text("def test_mine():\n    assert True\n")
    result = asyncio.run(box._t_run_tests({"path": "tests"}))
    assert result.ok is True, "pytest itself is green"
    assert result.data["contract_ok"] is False, "a missing contract file is a failure"
    assert "missing" in result.stdout


def test_the_run_tests_handler_gates_on_contract_ok() -> None:
    update = {}
    state = {"scratchpad": [], "retry_count": 0, "action": {"tool": "run_tests"},
             "observation": {"tool": "run_tests", "ok": False,
                             "data": {"pass_rate": 0.5, "full_suite": True, "contract_ok": True}}}
    out = asyncio.run(dev.dev_observe(state))
    update.update(out)
    assert update["tests_ok"] is True


def test_smoke_is_a_mandatory_gate() -> None:
    assert "smoke_ok" in dev.GATE_TOOLS
    assert dev.GATE_TOOLS["smoke_ok"] == "run_sandbox_smoke_test"


# ----------------------------------------------------------------------
# 3. a comparable target key, and the repeat warning it enables
# ----------------------------------------------------------------------

@pytest.mark.parametrize(("value", "key"), [
    ({"metric": "U", "reason": "utility is losing"}, "U"),
    ("F (active forgetting: deleted content cannot be recovered)", "F"),
    ("U (utility) on the standard_retrieval bucket", "U"),
    ("privacy leakage on role_mismatch", "A"),
    ("MGS", "MGS"),
    ("scoped_access_control", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_targets_metric_reduces_to_a_key(value, key) -> None:
    assert arch._metric_key(value) == key


def _designs(tmp_path: Path, monkeypatch, keys: list[str]) -> None:
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    for i, k in enumerate(keys, start=1):
        d = tmp_path / f"iter_{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "design.json").write_text(json.dumps({"targets_metric": {"metric": k}}))


def test_three_unpaid_designs_on_one_term_are_called_out(tmp_path: Path, monkeypatch) -> None:
    _designs(tmp_path, monkeypatch, ["U", "U", "U"])
    state = {"score_history": [
        {"iteration": 1, "MGS": 0.20}, {"iteration": 2, "MGS": 0.20}, {"iteration": 3, "MGS": 0.19},
    ]}
    warning = arch._repeat_target_warning(state, iteration=4)
    assert "ALL TARGETED U" in warning
    assert "1, 2, 3" in warning


def test_the_warning_is_silent_when_the_term_paid_or_changed(tmp_path: Path, monkeypatch) -> None:
    _designs(tmp_path, monkeypatch, ["U", "U", "U"])
    paid = {"score_history": [{"iteration": 1, "MGS": 0.2}, {"iteration": 3, "MGS": 0.5}]}
    assert arch._repeat_target_warning(paid, iteration=4) == ""
    _designs(tmp_path, monkeypatch, ["U", "A", "U"])
    assert arch._repeat_target_warning({"score_history": []}, iteration=4) == ""
    _designs(tmp_path, monkeypatch, ["U", "U"])
    assert arch._repeat_target_warning({"score_history": []}, iteration=3) == "", "needs three"


# ----------------------------------------------------------------------
# 1. the notebook gets its own call
# ----------------------------------------------------------------------

def test_the_notebook_call_returns_the_notes(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    async def fake(profile, task, workdir, timeout_s=None, **kw):
        seen["task"] = task
        return (SimpleNamespace(error=None, finish_reason="completed"),
                {"notebook_notes": ["seed scored 0.18", " ", "grants for 7 roles landed"]})

    monkeypatch.setattr(arch, "agent_call_json", fake)
    notes = asyncio.run(arch._notebook_call(
        3, ["old note"], "the critique", {"targets_metric": {"metric": "U"}, "work_order": ["s1"]},
        tmp_path,
    ))
    assert notes == ["seed scored 0.18", "grants for 7 roles landed"], "blank notes dropped"
    assert '"notebook_notes"' in seen["task"].split("## ")[0], "the required shape comes FIRST"
    assert "old note" in seen["task"] and "the critique" in seen["task"]


def test_the_notebook_call_tolerates_a_second_miss(monkeypatch, tmp_path: Path) -> None:
    async def fake(profile, task, workdir, timeout_s=None, **kw):
        return (SimpleNamespace(error="timeout", finish_reason=None), {})

    monkeypatch.setattr(arch, "agent_call_json", fake)
    assert asyncio.run(arch._notebook_call(1, [], "", {}, tmp_path)) == []


def test_config_defaults_for_the_fixes() -> None:
    # EVALUATE_SEED is True in production; conftest forces it off under test.
    assert config.ARCHITECT_NOTEBOOK_CALL is True
    assert config.DEVELOPER_MAX_TURNS >= 80
    assert DevToolbox.READ_FILE_LINES >= 1_000


def test_a_workspace_that_never_had_the_seed_tests_has_no_contract(tmp_path: Path, monkeypatch) -> None:
    """A fixture workspace with only its own passing test is not 'missing' anything."""
    seed = tmp_path / "seed"
    (seed / "tests").mkdir(parents=True)
    (seed / "tests" / "test_contract.py").write_text("def test_contract():\n    assert True\n")
    monkeypatch.setattr(config, "TEMPLATES_DIR", seed)
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    result = asyncio.run(DevToolbox(workspace=ws)._t_run_tests({"path": "tests"}))
    assert result.ok is True
    assert result.data["contract_ok"] is True


# ----------------------------------------------------------------------
# the Architect's reasoning budget: a starved reply is retried bigger, once,
# and a failed design call does not consume the iteration number
# ----------------------------------------------------------------------

class _Reply:
    def __init__(self, text, finish_reason, usage=None):
        self.ok = True; self.text = text; self.content = text; self.tool_calls = []
        self.finish_reason = finish_reason; self.error = None
        self.usage = usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_a_starved_reply_is_retried_once_with_a_doubled_budget(monkeypatch) -> None:
    import dataclasses

    import llm
    from harness.profiles import ARCHITECT_PROFILE
    from nodes import _transport

    calls: list[int | None] = []

    class _Client:
        async def chat(self, **kw):
            calls.append(kw.get("max_tokens"))
            if len(calls) == 1:
                return _Reply("", "length", {"input_tokens": 30, "output_tokens": 1000, "total_tokens": 1030})
            return _Reply("here is the design", "stop")

    monkeypatch.setattr(llm, "get_llm_client", lambda: _Client())
    monkeypatch.setattr(config, "ARCHITECT_TRANSPORT", "http")
    monkeypatch.setattr(config, "LLM_LENGTH_RETRY_CEILING", 131_072)
    profile = dataclasses.replace(ARCHITECT_PROFILE, max_tokens=1000)

    result = asyncio.run(_transport.agent_call(profile, "design it", Path("."), timeout_s=30))

    assert calls == [1000, 2000], "one retry, at double the budget"
    assert result.ok and result.text == "here is the design"
    assert result.usage["output_tokens"] == 1005, "both attempts are billed"


def test_a_starved_reply_is_not_retried_past_the_ceiling(monkeypatch) -> None:
    import dataclasses

    import llm
    from harness.profiles import ARCHITECT_PROFILE
    from nodes import _transport

    calls: list[int | None] = []

    class _Client:
        async def chat(self, **kw):
            calls.append(kw.get("max_tokens"))
            return _Reply("", "length")

    monkeypatch.setattr(llm, "get_llm_client", lambda: _Client())
    monkeypatch.setattr(config, "ARCHITECT_TRANSPORT", "http")
    monkeypatch.setattr(config, "LLM_LENGTH_RETRY_CEILING", 1000)
    profile = dataclasses.replace(ARCHITECT_PROFILE, max_tokens=1000)

    result = asyncio.run(_transport.agent_call(profile, "design it", Path("."), timeout_s=30))

    assert calls == [1000], "already at the ceiling: no retry"
    assert result.ok is False and "finish_reason=length" in (result.error or "")


def test_a_failed_architect_call_does_not_advance_the_iteration() -> None:
    """The failure return carries no `iteration_count`, so the retry designs the
    same iteration instead of skipping to the next number."""
    import inspect

    src = inspect.getsource(arch.architect_node)
    failure = src[src.index('log.error("architect failed'):]
    failure = failure[: failure.index("}")]
    assert "halt_reason" in failure
    assert '"iteration_count"' not in failure


# ----------------------------------------------------------------------
# a degraded stage: outage halts, an unservable build is a failed round
# ----------------------------------------------------------------------

def _degraded_state(**over):
    import state as st
    s = dict(st.initial_state(workspace="w", started_at=0.0))
    s.update({"render_degraded": True, "n_render_degraded": 292, "render_degraded_rate": 0.84,
              "n_checkpoints_evaluated": 547, "iteration_count": 17})
    s.update(over)
    return s


def test_a_degraded_stage_with_a_dead_endpoint_halts(monkeypatch) -> None:
    import routers
    monkeypatch.setattr(config, "MAX_ITERATIONS", 20)
    assert routers.route_after_collect(_degraded_state(evaluator_reachable=False)) == "halt"


def test_a_degraded_stage_with_a_live_endpoint_is_a_failed_round(monkeypatch) -> None:
    import routers
    monkeypatch.setattr(config, "MAX_ITERATIONS", 20)
    assert routers.route_after_collect(_degraded_state(evaluator_reachable=True)) == "architect"
    monkeypatch.setattr(config, "MAX_ITERATIONS", 17)
    assert routers.route_after_collect(_degraded_state(evaluator_reachable=True)) == "halt", \
        "a failed round still respects the iteration budget"


def test_the_unservable_report_names_the_servers_reason_and_the_prompt_size() -> None:
    from nodes.medical_evaluator import unservable_build_report

    r = unservable_build_report(
        17, 292, 346, kinds={"context_overflow": 292}, prompt_chars=[606_791, 779_729, 1_264_092],
        sample="This model's maximum context length is 131072 tokens.",
    )
    assert r["classification"] == "unservable"
    assert r["render_error_kind"] == "context_overflow"
    assert "too long for its context window" in r["reason"]
    assert "max 1,264,092" in r["classification_reason"]
    assert "131072" in r["classification_reason"], "the server's own words are quoted"


def test_a_silent_failure_is_reported_as_no_response() -> None:
    from nodes.medical_evaluator import unservable_build_report

    r = unservable_build_report(3, 10, 12, kinds={"no_response": 10}, prompt_chars=[])
    assert r["render_error_kind"] == "no_response" and "probe" in r["reason"]
    assert "(not recorded)" in r["classification_reason"]


def test_a_context_overflow_is_recognised_and_not_retried() -> None:
    from nodes.medical_evaluator import _is_context_overflow, _render_error_kind

    msg = ("HTTP 400: This model's maximum context length is 131072 tokens. However, you "
           "requested 4096 output tokens and your prompt contains at least 126977 input tokens")
    assert _is_context_overflow(msg)
    assert _render_error_kind(msg) == "context_overflow"
    assert _render_error_kind("HTTP 429: slow down") == "rejected"
    assert _render_error_kind("") == "no_response"
    assert not _is_context_overflow("ReadTimeout")


def test_the_architect_is_told_the_build_was_unservable() -> None:
    from nodes.medical_evaluator import unservable_build_report

    block = arch._developer_failure_block({"dev_failure_report":
        unservable_build_report(17, 292, 346, kinds={"context_overflow": 292}, prompt_chars=[900_000])})
    assert "BUILT, BUT COULD NOT BE EVALUATED" in block
    assert "rolled back to the champion" in block
    assert "COULD NOT BUILD" not in block


def test_the_probe_is_skipped_in_mock_mode(monkeypatch) -> None:
    from nodes.medical_evaluator import probe_evaluator
    monkeypatch.setattr(config, "MOCK_MODE", True)
    assert asyncio.run(probe_evaluator()) is True


# ----------------------------------------------------------------------
# --resume-from: the loop's state rebuilt from disk
# ----------------------------------------------------------------------

def _fake_run(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    for i, mgs in ((0, 0.0), (1, 0.30), (2, 0.25), (3, None)):
        d = runs / f"iter_{i}"
        (d / "workspace" / "memory_system").mkdir(parents=True)
        (d / "workspace" / "memory_system" / "schema.sql").write_text(f"-- schema {i}\n")
        if mgs is not None:
            (d / "dev").mkdir()
            (d / "dev" / "judge_report.json").write_text(json.dumps(
                {"U": mgs, "A": 0.0, "F": 0.0, "MGS": mgs, "n_scored": 10}))
            (d / "critique.md").write_text(f"critique of {i}")
            (d / "attribution.json").write_text(json.dumps({"dominant_term": "U", "it": i}))
    (runs / "notebook_history.md").write_text(
        "# NOTEBOOK HISTORY\n\n## after iteration 1\n- note one\n\n"
        "## after iteration 2\n- note A\n- note B\n\n## after iteration 3\n- note from the abandoned round\n")
    (runs / "critique_summary.md").write_text("# CRITIQUE SUMMARY\n\n- note from the abandoned round\n")
    return runs


def test_resume_rebuilds_state_and_moves_later_rounds_aside(tmp_path: Path, monkeypatch) -> None:
    import main
    import state as st

    runs = _fake_run(tmp_path)
    monkeypatch.setattr(config, "RUNS_DIR", runs)
    s = main.resume_state(dict(st.initial_state(workspace="w", started_at=0.0)), 2)

    assert s["iteration_count"] == 2
    assert [r["iteration"] for r in s["score_history"]] == [0, 1, 2]
    assert s["critique"] == "critique of 2" and s["critique_iteration"] == 2
    assert s["attribution"]["it"] == 2
    assert s["notebook_notes"] == ["note A", "note B"], "the notebook as it stood after iteration 2"
    assert "note A" in (runs / "critique_summary.md").read_text()
    assert not (runs / "iter_3").exists(), "the later round is moved aside..."
    assert any(p.name.startswith("iter_3_abandoned_") for p in runs.iterdir()), "...not deleted"
    import scoreboard
    assert scoreboard.best_of(s) == (0.30, 1), "the champion comes from the rebuilt history"


def test_the_budget_end_of_a_failed_round_is_named_honestly(monkeypatch) -> None:
    import routers
    monkeypatch.setattr(config, "MAX_ITERATIONS", 20)
    s = _degraded_state(iteration_count=20, evaluator_reachable=True,
                        dev_failure_report={"render_error_kind": "context_overflow"})
    reason = routers.halt_reason_for(s)
    assert "budget exhausted after 20" in reason and "context_overflow" in reason
    assert "would not describe" not in reason


# ----------------------------------------------------------------------
# answerer visibility: the Critic sees the exchange, not just a counter
# ----------------------------------------------------------------------

def test_the_critic_facts_carry_the_exchange(tmp_path: Path) -> None:
    from nodes import critic as cr
    p = tmp_path / "predictions.jsonl"
    p.write_text(json.dumps({"checkpoint_id": "c1", "output": {
        "action": "refuse", "answer": "", "used_record_ids": ["r1"],
        "answerer_reply": '{"action": "refuse", "answer": ""} I cannot share clinical details.',
        "evidence_block": "Memory 1 (speaker=patient_ana): Stop ibuprofen; start acetaminophen.",
        "debug": {"retrieval": {"n_allowed": 8}}}}) + "\n")
    facts = cr._prediction_facts(str(p))
    assert "cannot share clinical details" in facts["c1"]["answerer_reply"]
    assert "speaker=patient_ana" in facts["c1"]["evidence_block"]


def test_the_observed_block_quotes_the_exchange_for_cleared_but_withheld(monkeypatch) -> None:
    from nodes import critic as cr
    monkeypatch.setattr(config, "CRITIC_ANSWERER_EXCERPTS", 1)
    facts = {
        "c1": {"action": "refuse", "answer_text": False, "n_used": 1,
               "retrieval": {"n_allowed": 8, "n_denied_tombstone": 0, "n_denied_rbac": 0, "n_denied_scope": 0},
               "answerer_reply": "I cannot share clinical details with a family member.",
               "evidence_block": "Memory 1 (speaker=clinician): the dose is 650 mg."},
        "c2": {"action": "refuse", "answer_text": False, "n_used": 1,
               "retrieval": {"n_allowed": 8, "n_denied_tombstone": 0, "n_denied_rbac": 0, "n_denied_scope": 0},
               "answerer_reply": "SECOND REPLY", "evidence_block": "x"},
        "c3": {"action": "refuse", "answer_text": False, "n_used": 0,
               "retrieval": {"n_allowed": 0, "n_denied_tombstone": 0, "n_denied_rbac": 4, "n_denied_scope": 0},
               "answerer_reply": "GATE-DENIED REPLY", "evidence_block": ""},
    }
    block = cr._observed_block([{"checkpoint_id": c} for c in ("c1", "c2", "c3")], facts)
    assert "the answerer replied:" in block and "cannot share clinical details" in block
    assert "the dose is 650 mg" in block, "the evidence it was shown is quoted too"
    assert "SECOND REPLY" not in block, "excerpts are capped by CRITIC_ANSWERER_EXCERPTS"
    assert "GATE-DENIED REPLY" not in block, "only checkpoints the gate cleared are quoted"


# ----------------------------------------------------------------------
# the probe gate
# ----------------------------------------------------------------------

def _probe_state(mgs, history_best=None, **over):
    import state as st
    s = dict(st.initial_state(workspace="w", started_at=0.0))
    s.update({"eval_stage": "probe", "mgs_score": mgs, "iteration_count": 5,
              "judge_report": {"U": mgs, "A": 0.0, "F": 0.0, "phase_score": 1.0}})
    if history_best is not None:
        s["score_history"] = [{"iteration": 3, "stage": "probe", "MGS": history_best, "U": 0, "A": 0, "F": 0},
                              {"iteration": 3, "stage": "dev", "MGS": history_best, "U": 0, "A": 0, "F": 0}]
    s.update(over)
    return s


def test_a_probe_within_tolerance_goes_on_to_the_full_slice(monkeypatch) -> None:
    import routers
    monkeypatch.setattr(config, "PROBE_TOLERANCE", 0.10)
    assert routers.route_after_judge(_probe_state(0.45, history_best=0.50)) == "stage_dev"
    assert routers.route_after_judge(_probe_state(0.10)) == "stage_dev", "no prior probe: nothing to fail against"


def test_a_collapsed_or_degraded_probe_ends_the_round(monkeypatch) -> None:
    import routers
    monkeypatch.setattr(config, "PROBE_TOLERANCE", 0.10)
    assert routers.route_after_judge(_probe_state(0.30, history_best=0.50)) == "probe_fail"
    assert routers.route_after_judge(_probe_state(0.50, history_best=0.50, render_degraded=True)) == "probe_fail"


def test_the_probe_bar_is_the_champions_score_not_the_best_probe_ever(monkeypatch) -> None:
    """Iteration 4 probed 0.90 and collapsed to 0.10 on the full slice; iteration 3
    (probe 0.50, dev 0.90) is the champion. A 0.45 probe must pass."""
    import routers
    monkeypatch.setattr(config, "PROBE_TOLERANCE", 0.10)
    s = _probe_state(0.45, history_best=0.50)
    s["score_history"] += [{"iteration": 4, "stage": "probe", "MGS": 0.90, "U": 0, "A": 0, "F": 0},
                           {"iteration": 4, "stage": "dev", "MGS": 0.10, "U": 0, "A": 0, "F": 0}]
    assert routers.route_after_judge(s) == "stage_dev"


def test_a_failed_probe_still_spends_the_iteration_budget(monkeypatch) -> None:
    import routers
    monkeypatch.setattr(config, "MAX_ITERATIONS", 20)
    s = _probe_state(0.10, history_best=0.50, iteration_count=19)
    assert routers.route_after_probe_fail(s) == "architect"
    s["iteration_count"] = 20
    assert routers.route_after_probe_fail(s) == "halt"


def test_the_probe_is_a_fixed_subset_of_the_dev_slice(monkeypatch) -> None:
    from gatemem_adapter import select_dev_slice
    cps = [{"checkpoint_id": f"c{i:03d}", "query_type": ("utility", "privacy", "safety")[i % 3]} for i in range(300)]
    dev = select_dev_slice(cps, n=100, seed=7)
    probe = select_dev_slice(dev, n=40, seed=7)
    again = select_dev_slice(select_dev_slice(cps, n=100, seed=7), n=40, seed=7)
    assert len(probe) == 40
    assert {c["checkpoint_id"] for c in probe} <= {c["checkpoint_id"] for c in dev}
    assert [c["checkpoint_id"] for c in probe] == [c["checkpoint_id"] for c in again], "deterministic"
    assert len({c["query_type"] for c in probe}) == 3, "stratified: every query type present"


def test_probe_fail_writes_a_report_the_architect_can_read(monkeypatch) -> None:
    import graph
    monkeypatch.setattr(config, "PROBE_TOLERANCE", 0.10)
    s = _probe_state(0.30, history_best=0.50)
    out = asyncio.run(graph.probe_fail_node(s))
    rep = out["dev_failure_report"]
    assert rep["classification"] == "probe" and "below 0.4000" in rep["reason"]
    assert out["dev_failure_history"][0]["signature"] == "probe-collapse"
    block = arch._developer_failure_block({"dev_failure_report": rep})
    assert "FAILED THE PROBE" in block and "rolled back to the champion" in block


def test_stage_select_probes_only_when_enabled(monkeypatch) -> None:
    import graph
    monkeypatch.setattr(config, "PROBE_CHECKPOINTS", 40)
    assert asyncio.run(graph.stage_select_node({}))["eval_stage"] == "probe"
    monkeypatch.setattr(config, "PROBE_CHECKPOINTS", 0)
    assert asyncio.run(graph.stage_select_node({}))["eval_stage"] == "dev"


# ---------------------------------------------------------------------------
# The keyring file is frozen for the Developer
# ---------------------------------------------------------------------------


def test_the_keyring_cannot_be_written_or_patched(tmp_path: Path) -> None:
    box = DevToolbox(workspace=tmp_path)
    (tmp_path / "memory_system").mkdir()
    (tmp_path / "memory_system" / "keyring.py").write_text("FROZEN = True\n")
    for tool, args in (
        ("write_file", {"path": "memory_system/keyring.py", "content": "FROZEN = False\n"}),
        ("apply_patch", {"path": "/memory_system/keyring.py", "patch": "--- a\n+++ b\n"}),
    ):
        result = asyncio.run(box.call(tool, args))
        assert not result.ok and "frozen" in result.stderr
    assert (tmp_path / "memory_system" / "keyring.py").read_text() == "FROZEN = True\n"
    ok = asyncio.run(box.call("write_file", {"path": "memory_system/store.py", "content": "x = 1\n"}))
    assert ok.ok, ok.stderr


def test_the_recursion_limit_scales_with_the_iteration_budget() -> None:
    """250 supersteps ended two 20-iteration runs at iteration 17."""
    assert config.RECURSION_LIMIT >= (config.MAX_ITERATIONS + 1) * 24 + 50


def test_the_recursion_limit_follows_a_late_max_iterations(monkeypatch) -> None:
    """`--max-iterations` rewrites MAX_ITERATIONS after import; the limit must follow."""
    monkeypatch.setattr(config, "MAX_ITERATIONS", 20)
    assert config.recursion_limit() >= 21 * 24 + 50


# ---------------------------------------------------------------------------
# the Critic sees whether a build changed anything the benchmark measures
# ---------------------------------------------------------------------------


def _write_predictions(path: Path, rows: dict[str, tuple[str, list[str]]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for cid, (action, used) in rows.items():
            fh.write(json.dumps({"checkpoint_id": cid,
                                 "output": {"action": action, "used_record_ids": used}}) + "\n")
    return str(path)


def test_an_unchanged_measurement_is_named_as_such(tmp_path: Path) -> None:
    from nodes.critic import measurement_delta
    rows = {"c1": ("answer", ["r1", "r2"]), "c2": ("refuse", []), "c3": ("no_memory", [])}
    parent = _write_predictions(tmp_path / "p" / "predictions.jsonl", rows)
    same = _write_predictions(tmp_path / "n" / "predictions.jsonl",
                              {k: (a, list(reversed(u))) for k, (a, u) in rows.items()})
    text = measurement_delta(same, parent, 4)
    assert text.startswith("THIS BUILD CHANGED NOTHING THE BENCHMARK MEASURES") and "3 checkpoints" in text


def test_a_changed_measurement_is_counted(tmp_path: Path) -> None:
    from nodes.critic import measurement_delta
    parent = _write_predictions(tmp_path / "p" / "predictions.jsonl",
                                {"c1": ("answer", ["r1"]), "c2": ("refuse", [])})
    now = _write_predictions(tmp_path / "n" / "predictions.jsonl",
                             {"c1": ("answer", ["r1", "r9"]), "c2": ("answer", ["r2"])})
    text = measurement_delta(now, parent, 4)
    assert "action changed on 1/2" in text and "released records changed on 2/2" in text
    assert measurement_delta(now, str(tmp_path / "missing.jsonl"), 4) == ""


# ---------------------------------------------------------------------------
# facts in the released evidence
# ---------------------------------------------------------------------------


def test_evidence_coverage_separates_not_released_from_not_answered() -> None:
    from nodes.judge import evidence_coverage, failure_buckets
    ann = {"query_type": "utility", "expected_action": "answer",
           "judge_spec": {"include": ["236,000 USD", "10%"]}}
    turns = {"1": "Approved budget is 236,000 USD.", "2": "Max discount is 10%.", "3": "Meeting moved."}
    # released the budget turn, answered the budget, missed the discount -- which
    # sits in a stored turn that was NOT released: fact_not_released.
    partial = {"output": {"action": "answer", "answer": "Budget is 236,000 USD.", "used_record_ids": ["1", "3"],
                          "debug": {"retrieval": {"n_allowed": 2}}}}
    p = evidence_coverage(partial, ann, turns)
    assert p["evidence_covers_facts"] is False and p["missing_from_evidence"] == ["10%"]
    # released both fact turns, answered neither: the facts were in hand.
    withheld = {"output": {"action": "refuse", "answer": "", "used_record_ids": ["1", "2"],
                           "debug": {"retrieval": {"n_allowed": 2}}}}
    assert evidence_coverage(withheld, ann, turns)["evidence_covers_facts"] is True
    # a pattern phrased for answers that matches NO stored turn proves nothing.
    ann2 = {"query_type": "utility", "judge_spec": {"include": ["two hundred thirty-six thousand"]}}
    assert evidence_coverage(partial, ann2, turns)["evidence_covers_facts"] is True
    unknown = {"output": {"action": "answer", "answer": "", "used_record_ids": ["chunk_9"]}}
    assert evidence_coverage(unknown, ann, turns)["evidence_covers_facts"] is None
    verdicts = {"a": {"utility_correct": False, "evidence_covers_facts": True},
                "b": {"utility_correct": False, "evidence_covers_facts": False}}
    buckets = failure_buckets(verdicts, {"a": withheld, "b": partial}, {"a": ann, "b": ann})
    assert buckets["counts"] == {"withheld_other": 1, "fact_not_released": 1}


def test_the_critic_names_a_fact_that_was_never_released() -> None:
    from nodes.critic import _classify
    fact = {"action": "answer", "answer_text": True, "retrieval": {"n_allowed": 5},
            "action_correct": True, "missed_patterns": ["10%"], "evidence_covers_facts": False}
    assert _classify(fact) == "fact_not_released"
    fact["evidence_covers_facts"] = True
    assert _classify(fact) == "answered_but_content_missing"


# ---------------------------------------------------------------------------
# the task template: no seed system, a task and a census instead
# ---------------------------------------------------------------------------

TASK_TEMPLATE = Path(__file__).resolve().parents[1] / "templates_task"


def test_the_task_template_is_a_task_not_a_system(monkeypatch) -> None:
    from nodes.developer import is_unbuilt
    monkeypatch.setattr(config, "TEMPLATES_DIR", TASK_TEMPLATE)
    assert config.task_template()
    assert is_unbuilt(TASK_TEMPLATE), "the stub must read as unbuilt"
    assert not is_unbuilt(Path(__file__).resolve().parents[1] / "templates_generic")
    assert (TASK_TEMPLATE / "memory_system" / "keyring.py").is_file()
    assert "grant_by_policy" not in (TASK_TEMPLATE / "memory_system" / "keyring.py").read_text(), \
        "the frozen keyring carries no policy"
    monkeypatch.setattr(config, "TEMPLATES_DIR", Path("/nonexistent"))
    assert not config.task_template()


def test_a_build_round_gets_the_build_budget(monkeypatch) -> None:
    import nodes.developer as dev
    monkeypatch.setattr(config, "DEVELOPER_MAX_TURNS", 80)
    monkeypatch.setattr(config, "DEVELOPER_BUILD_TURNS", 160)
    monkeypatch.setattr(dev, "_BUILD_ROUND", True)
    assert dev._max_turns() == 160
    monkeypatch.setattr(dev, "_BUILD_ROUND", False)
    assert dev._max_turns() == 80


def test_the_brief_carries_the_task_and_nothing_about_the_data(monkeypatch) -> None:
    """On a task template the Architect is told the task verbatim and NOT
    given a summary of the dataset: the memory reads its own episode."""
    from nodes.architect import build_framing, task_block
    monkeypatch.setattr(config, "TEMPLATES_DIR", TASK_TEMPLATE)
    monkeypatch.setattr(config, "MOCK_MODE", False)
    block = task_block(TASK_TEMPLATE)
    assert "## The interface (fixed)" in block and "grants are decided at write time" in block
    assert "Relationship types" not in block and "expects of them" not in block, \
        "the brief carries no dataset census"
    assert "family_contact" not in block and "fixture_" not in block
    assert build_framing(TASK_TEMPLATE).startswith("THIS ITERATION IS THE FIRST BUILD")
    assert build_framing(Path(__file__).resolve().parents[1] / "templates_generic").startswith("THE WORK ORDER IS A DELTA")



def test_a_build_round_gets_the_build_retry_cap(monkeypatch) -> None:
    import nodes.developer as dev
    monkeypatch.setattr(config, "MAX_DEV_RETRIES", 5)
    monkeypatch.setattr(config, "DEVELOPER_BUILD_RETRIES", 15)
    monkeypatch.setattr(dev, "_BUILD_ROUND", True)
    assert dev._retry_cap() == 15
    monkeypatch.setattr(dev, "_BUILD_ROUND", False)
    assert dev._retry_cap() == 5


def test_sql_exec_without_a_schema_file_checks_the_migration_alone(tmp_path: Path) -> None:
    box = DevToolbox(workspace=tmp_path)
    (tmp_path / "memory_system").mkdir()
    box.migration_sql = ""
    ok = asyncio.run(box.call("sql_exec", {}))
    assert ok.ok and "schema lives in code" in ok.stdout, ok.stderr
    good = asyncio.run(box.call("sql_exec", {"migration_sql": "CREATE TABLE t(x INT); CREATE INDEX ix ON t(x);"}))
    assert good.ok and good.data["tables"] == ["t"]
    bad = asyncio.run(box.call("sql_exec", {"migration_sql": "ALTER TABLE nope ADD COLUMN y INT;"}))
    assert not bad.ok


def test_turns_by_record_id_matches_bare_turn_ids_too() -> None:
    from types import SimpleNamespace

    from nodes.judge import turns_by_record_id
    ds = SimpleNamespace(episodes_by_id={"ep": {"turns": [{"turn_id": "t7", "text": "budget 236,000 USD"}]}})
    m = turns_by_record_id(ds, "ep")
    assert m["ep:t7"] == "budget 236,000 USD" and m["t7"] == "budget 236,000 USD"
    assert m["7"] == "budget 236,000 USD"
    assert turns_by_record_id(ds, "") == {}


def test_every_mechanism_the_critic_can_name_has_a_meaning() -> None:
    """A mechanism `_classify` returns without a `_MECHANISM_MEANING` entry is a
    KeyError in the Critic's census -- 53 tests found it at once when
    `fact_not_released` was added to one and not the other."""
    import ast

    from nodes import critic
    src = ast.parse(Path(critic.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(src) if isinstance(n, ast.FunctionDef) and n.name == "_classify")
    returned = {n.value.value for n in ast.walk(fn) if isinstance(n, ast.Return)
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
    assert returned, "no string returns found"
    assert returned <= set(critic._MECHANISM_MEANING), returned - set(critic._MECHANISM_MEANING)


def test_the_task_template_protects_the_harness_files() -> None:
    assert {"memory_system/keyring.py", "memory_system/llm.py"} <= set(DevToolbox.PROTECTED_FILES)
    assert (TASK_TEMPLATE / "memory_system" / "llm.py").is_file()


def test_the_memory_llm_call_is_budgeted_cached_and_off_without_an_endpoint(monkeypatch) -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("task_llm", TASK_TEMPLATE / "memory_system" / "llm.py")
    llm = importlib.util.module_from_spec(spec); spec.loader.exec_module(llm)
    monkeypatch.delenv("MEMORY_LLM_BASE_URL", raising=False)
    assert llm.complete("anything") is None and not llm.enabled()
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://fake/v1")
    monkeypatch.setenv("MEMORY_LLM_CALL_BUDGET", "2")
    calls = []

    class _Resp:
        def __init__(self, text): self._b = json.dumps({"choices": [{"message": {"content": text}}],
                                                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(json.loads(req.data)["messages"][-1]["content"])
        return _Resp("yes")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    llm.new_episode()
    assert llm.complete("p1") == "yes" and llm.complete("p1") == "yes" and len(calls) == 1, "cached"
    assert llm.complete("p2") == "yes" and len(calls) == 2
    assert llm.complete("p3") is None and len(calls) == 2, "over the call budget"
    u = llm.usage()
    assert u["calls"] == 2 and u["cache_hits"] == 1 and u["refused_over_budget"] == 1 and u["prompt_tokens"] == 20
    llm.new_episode()
    assert llm.usage()["calls"] == 0


def test_a_build_round_gets_the_build_think_budget(monkeypatch) -> None:
    import nodes.developer as dev
    monkeypatch.setattr(config, "DSH_DEVELOPER_THINK_TIMEOUT_S", 300.0)
    monkeypatch.setattr(config, "DSH_DEVELOPER_BUILD_THINK_TIMEOUT_S", 900.0)
    monkeypatch.setattr(dev, "_BUILD_ROUND", True)
    assert dev._think_budget_s() == 900.0 and dev._sample_timeout(0, 3) == 450
    monkeypatch.setattr(dev, "_BUILD_ROUND", False)
    assert dev._think_budget_s() == 300.0 and dev._sample_timeout(0, 3) == 150


def test_a_near_tie_keeps_the_newer_build_as_parent(monkeypatch) -> None:
    import scoreboard
    monkeypatch.setattr(config, "ROLLBACK_TO_BEST", True)
    monkeypatch.setattr(config, "LINEAGE_TIE_EPS", 0.01)
    hist = [{"iteration": 8, "stage": "dev", "MGS": 0.1898, "U": 0, "A": 0, "F": 0},
            {"iteration": 9, "stage": "dev", "MGS": 0.1500, "U": 0, "A": 0, "F": 0},
            {"iteration": 10, "stage": "dev", "MGS": 0.1866, "U": 0, "A": 0, "F": 0}]
    assert scoreboard.lineage_parent({"score_history": hist}, fallback=10)[0] == 10
    assert scoreboard.best_of({"score_history": hist})[1] == 8, "the champion is unchanged"
    hist[2]["MGS"] = 0.1700
    assert scoreboard.lineage_parent({"score_history": hist}, fallback=10)[0] == 8
    monkeypatch.setattr(config, "LINEAGE_TIE_EPS", 0.0)
    hist[2]["MGS"] = 0.1866
    assert scoreboard.lineage_parent({"score_history": hist}, fallback=10)[0] == 8


def test_the_probe_tolerance_widens_with_the_measured_probe_gap(monkeypatch) -> None:
    import routers
    import scoreboard
    monkeypatch.setattr(config, "PROBE_TOLERANCE", 0.10)
    s = _probe_state(0.50, history_best=0.72)
    assert scoreboard.probe_tolerance(s) >= 0.10
    # the office adaptive run: probe 0.72 / dev 0.46 on the champion, 0.54 / 0.43 on another
    s["score_history"] = [
        {"iteration": 1, "stage": "probe", "MGS": 0.54, "U": 0, "A": 0, "F": 0},
        {"iteration": 1, "stage": "dev", "MGS": 0.43, "U": 0, "A": 0, "F": 0},
        {"iteration": 3, "stage": "probe", "MGS": 0.72, "U": 0, "A": 0, "F": 0},
        {"iteration": 3, "stage": "dev", "MGS": 0.46, "U": 0, "A": 0, "F": 0},
    ]
    tol = scoreboard.probe_tolerance(s)
    assert abs(tol - 2 * ((0.11 + 0.26) / 2)) < 1e-9
    assert routers.route_after_judge(s) == "stage_dev", "0.50 is within the measured noise of 0.72"
    s["mgs_score"] = 0.20
    assert routers.route_after_judge(s) == "probe_fail"


def test_task_agent_debug_is_split_not_nested() -> None:
    from nodes.medical_evaluator import split_agent_debug
    task = {"retrieval": {"n_allowed": 29}, "memory_llm": {"calls": 3}}
    assert split_agent_debug(task) == {"retrieval": {"n_allowed": 29}, "memory_llm": {"calls": 3}}
    seed = {"agent": "x", "n_allowed": 4}
    assert split_agent_debug(seed) == {"retrieval": seed}
    assert split_agent_debug(None) == {"retrieval": {}}


def test_memory_llm_totals_and_the_critic_line() -> None:
    from nodes.critic import _memory_llm_line
    from nodes.judge import memory_llm_totals
    preds = {"a": {"output": {"debug": {"memory_llm": {"calls": 3, "prompt_tokens": 100, "completion_tokens": 10}}}},
             "b": {"output": {"debug": {"memory_llm": {"calls": 0}}}},
             "c": {"output": {"debug": {}}}}
    t = memory_llm_totals(preds)
    assert t["checkpoints_using"] == 1 and t["calls_sum"] == 3 and t["tokens_sum"] == 110
    assert "used on 1 checkpoint" in _memory_llm_line({"memory_llm_totals": t})
    assert _memory_llm_line({}).startswith("not used")


def test_the_adaptivity_gate_is_off_in_mock_and_without_an_endpoint(tmp_path: Path, monkeypatch) -> None:
    box = DevToolbox(workspace=tmp_path)
    monkeypatch.setattr(config, "MOCK_MODE", True)
    assert box._memory_env() == {}
    assert asyncio.run(box._adaptivity_checks()) == {"ran": False}
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "TEMPLATES_DIR", TASK_TEMPLATE)
    monkeypatch.setattr(config, "MEMORY_LLM_BASE_URL", "")
    assert asyncio.run(box._adaptivity_checks()) == {"ran": False}
    assert config.ADAPTIVITY_CHECKS.is_file() and not (TASK_TEMPLATE / "tests" / "test_adaptivity.py").exists(), \
        "the held-out checks live outside the template"


def test_the_critic_prints_the_memorys_own_counters() -> None:
    from nodes.critic import _observed_block
    facts = {"c1": {"action": "answer", "answer_text": True, "n_used": 3,
                    "retrieval": {"n_allowed": 29, "n_revoked_holders": 2},
                    "memory_llm": {"calls": 4}}}
    block = _observed_block([{"checkpoint_id": "c1"}], facts)
    assert "n_allowed=29" in block and "n_revoked_holders=2" in block and "memory_llm_calls=4" in block
    assert "denied_rbac=?" not in block


def test_leak_channels_split_released_from_answer_only() -> None:
    from nodes.critic import _leak_channel_line
    from nodes.judge import leak_channels
    verdicts = {"a": {"privacy_leak": True}, "b": {"privacy_leak": True}, "c": {"privacy_leak": False}}
    preds = {"a": {"output": {"action": "answer_redacted", "used_record_ids": ["r1"]}},
             "b": {"output": {"action": "no_memory", "used_record_ids": []}},
             "c": {"output": {"action": "answer", "used_record_ids": ["r2"]}}}
    ch = leak_channels(verdicts, preds)
    assert ch == {"released:answer_redacted": 1, "nothing_released:no_memory": 1}
    assert "1/2 leaks had records RELEASED" in _leak_channel_line({"leak_channels": ch})
    assert _leak_channel_line({}) == "no privacy leaks."


def test_a_provider_520_is_transient_everywhere() -> None:
    import routers
    from llm.client import RETRYABLE_STATUS
    assert {520, 522, 524, 529} <= RETRYABLE_STATUS
    s = {"halt_reason": 'architect invocation failed: HTTP 520: {"error":{"message":"Provider returned error"}}'}
    assert routers._architect_failure_is_transient(s)


def test_the_adaptivity_gate_needs_every_family(tmp_path: Path, monkeypatch) -> None:
    """13/18 with two families at zero must FAIL; parsing comes from pytest -rA lines."""
    import nodes.dev_tools as dt
    box = DevToolbox(workspace=tmp_path)
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "TEMPLATES_DIR", TASK_TEMPLATE)
    monkeypatch.setattr(config, "MEMORY_LLM_BASE_URL", "http://fake/v1")
    monkeypatch.setattr(config, "ADAPTIVITY_MIN_PASS", 0.5)
    monkeypatch.setattr(config, "ADAPTIVITY_EVERY_FAMILY", True)
    lines = ([f"PASSED x.py::test_consent_by_paraphrase[p{i}]" for i in range(4)]
             + [f"PASSED x.py::test_revocation_by_paraphrase[p{i}]" for i in range(4)]
             + [f"PASSED x.py::test_supersession_by_paraphrase[p{i}]" for i in range(3)]
             + ["FAILED x.py::test_supersession_by_paraphrase[p3]"]
             + [f"FAILED x.py::test_reference_by_name_reaches_the_holder[v{i}]" for i in range(2)]
             + [f"FAILED x.py::test_reference_by_name_does_not_cross_nested_names[v{i}]" for i in range(2)]
             + [f"PASSED x.py::test_consent_naming_people_by_display_name[v{i}]" for i in range(2)])
    out = "\n".join(lines) + "\n5 failed, 13 passed in 1.0s\n"

    async def fake_run(*a, **k):
        return 1, out, ""
    monkeypatch.setattr(dt, "_run", fake_run)
    r = asyncio.run(box._adaptivity_checks())
    assert r["ran"] and r["passed"] == 13 and r["total"] == 18
    assert not r["ok"] and r["zero_families"] == ["test_reference_by_name_does_not_cross_nested_names",
                                                   "test_reference_by_name_reaches_the_holder"]
    monkeypatch.setattr(config, "ADAPTIVITY_EVERY_FAMILY", False)
    assert asyncio.run(box._adaptivity_checks())["ok"]
