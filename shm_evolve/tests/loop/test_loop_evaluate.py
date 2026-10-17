"""evaluate.py: the field wall, one-pass checkpoint placement, the prediction
row shape, per-episode reuse and trace serialisation."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from contracts import HIDDEN_FIELDS
from tests.loop.conftest import DOMAIN, SENTINEL, STUB_AGENT


def _evaluate(dataset, cfg, client, prompts_dir: Path, out_dir: Path, agent_path: str = STUB_AGENT, **kw):
    from loop.evaluate import evaluate

    eids = [e["episode_id"] for e in dataset.episodes]
    return asyncio.run(evaluate(dataset, eids, prompts_dir, out_dir, client=client, cfg=cfg,
                                agent_path=agent_path, **kw))




def test_field_wall_agent_never_sees_annotations(loop_env, dataset, seed_prompts, tmp_path):
    cfg, client = loop_env
    res = _evaluate(dataset, cfg, client, seed_prompts, tmp_path / "ev")
    assert not res.episode_errors
    seen = (tmp_path / "seen.jsonl").read_text(encoding="utf-8")
    # The stub logged every episode, turn and checkpoint it was handed.
    assert '"kind": "query"' in seen and '"kind": "ingest"' in seen
    assert SENTINEL not in seen
    for line in seen.splitlines():
        obj = json.loads(line)["obj"]
        assert not (set(obj) & HIDDEN_FIELDS), obj
    stripped = (tmp_path / "ev" / "checkpoints.stripped.jsonl").read_text(encoding="utf-8")
    assert SENTINEL not in stripped
    assert not any(f'"{k}"' in stripped for k in HIDDEN_FIELDS)
    # ...while the scoring side still has the annotations.
    assert any(SENTINEL in json.dumps(cp) for cp in dataset.checkpoints)


def test_field_wall_blocks_opening_annotated_file(loop_env, dataset, seed_prompts, tmp_path, data_root,
                                                  monkeypatch):
    cfg, client = loop_env
    monkeypatch.setenv("SHM_STUB_TRY_OPEN", str(data_root / DOMAIN / "checkpoints.jsonl"))
    res = _evaluate(dataset, cfg, client, seed_prompts, tmp_path / "ev")
    assert set(res.episode_errors) == {e["episode_id"] for e in dataset.episodes}
    assert all("field wall" in err for err in res.episode_errors.values())
    # Crashed episodes still yield a scoreable row per checkpoint.
    assert len(res.predictions) == len(dataset.checkpoints)
    assert all(p["output"]["debug"].get("episode_error") for p in res.predictions)


def test_one_pass_checkpoint_placement(loop_env, dataset, seed_prompts, tmp_path):
    cfg, client = loop_env
    res = _evaluate(dataset, cfg, client, seed_prompts, tmp_path / "ev")
    by_id = {p["checkpoint_id"]: p for p in res.predictions}
    debug = {cid: p["output"]["debug"] for cid, p in by_id.items()}
    one, two = "zephyr_ep_one", "zephyr_ep_two"
    # before any turn: asked after reset with nothing ingested
    assert debug[f"{one}_ckpt_01"]["placed_after"] == ""
    assert debug[f"{one}_ckpt_01"]["agent"]["n_ingested"] == 0
    # mid-episode: right after its turn, not later
    assert debug[f"{one}_ckpt_03"]["placed_after"] == "t002"
    assert debug[f"{one}_ckpt_03"]["agent"]["n_ingested"] == 2
    assert debug[f"{one}_ckpt_04"]["agent"]["n_ingested"] == 1   # t001, though listed after a t002 checkpoint
    # equals the last turn
    assert debug[f"{one}_ckpt_06"]["placed_after"] == "t005"
    assert debug[f"{one}_ckpt_06"]["agent"]["n_ingested"] == 5
    # unmatched as_of after the end: asked after the last turn and reported
    assert debug[f"{two}_ckpt_05"]["placed_after"] == "t004"
    assert res.report["n_unplaced"] == 1
    # ONE pass: each episode was reset exactly once and each turn ingested once
    seen = [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text().splitlines()]
    assert sum(1 for s in seen if s["kind"] == "reset") == 2
    assert sum(1 for s in seen if s["kind"] == "ingest") == sum(len(e["turns"]) for e in dataset.episodes)
    assert len(res.predictions) == len(dataset.checkpoints)


def test_place_checkpoints_unit():
    from loop.data import place_checkpoints

    cps = [{"checkpoint_id": c, "as_of_turn_id": a} for c, a in
           [("a", "t000"), ("b", "t2"), ("c", "t10"), ("d", "zz"), ("e", "t2")]]
    before, after, unplaced = place_checkpoints(["t1", "t2", "t10"], cps)
    assert [c["checkpoint_id"] for c in before] == ["a"]
    assert [c["checkpoint_id"] for c in after["t2"]] == ["b", "e"]
    assert [c["checkpoint_id"] for c in after["t10"]] == ["c"]
    assert [c["checkpoint_id"] for c in unplaced] == ["d"]


def _bench_normaliser():
    """`_normalize_prediction_row` from bench/scripts/score_predictions.py,
    extracted by AST: the module itself imports sklearn via bench.eval."""
    from loop import config as config_mod

    path = config_mod.get().gatemem_repo / "bench" / "scripts" / "score_predictions.py"
    if not path.is_file():
        pytest.skip("bench/scripts/score_predictions.py not present")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "_normalize_prediction_row")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "TOP_LEVEL_OUTPUT_KEYS" for t in n.targets))]
    if not any(isinstance(n, ast.FunctionDef) for n in keep):
        pytest.skip("normaliser not found")
    ns: dict = {"Dict": dict, "Any": object, "List": list}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns)
    return ns["_normalize_prediction_row"]


def test_prediction_rows_are_bench_scoreable(loop_env, dataset, seed_prompts, tmp_path):
    cfg, client = loop_env
    res = _evaluate(dataset, cfg, client, seed_prompts, tmp_path / "ev")
    normalise = _bench_normaliser()
    rows = [json.loads(line) for line in (tmp_path / "ev" / "predictions.jsonl").read_text().splitlines()]
    assert len(rows) == len(dataset.checkpoints)
    for row in rows:
        # The nested form passes through the bench normaliser untouched.
        assert normalise(dict(row)) == row
        out = row["output"]
        assert {"action", "answer", "answer_structured", "used_record_ids"} <= set(out)
        assert out["action"] in ("answer", "answer_redacted", "refuse", "no_memory")
        assert {"checkpoint_id", "episode_id", "as_of_turn_id", "asker", "trace_seq", "render"} <= set(row)
    # the retriever's trace event for each checkpoint is linked from its row
    assert all(row["trace_seq"] for row in rows)


def test_traces_carry_episode_id_and_reuse(loop_env, dataset, seed_prompts, tmp_path):
    cfg, client = loop_env
    first = _evaluate(dataset, cfg, client, seed_prompts, tmp_path / "a")
    traces = [json.loads(line) for line in (tmp_path / "a" / "traces.jsonl").read_text().splitlines()]
    assert traces and all(t["episode_id"] in {"zephyr_ep_one", "zephyr_ep_two"} for t in traces)
    assert any(t["component"] == "retriever" and t["checkpoint_id"] for t in traces)
    again = _evaluate(dataset, cfg, client, seed_prompts, tmp_path / "b", reuse_from=[tmp_path / "a"])
    assert again.report["n_reused_episodes"] == 2 and again.report["n_run_episodes"] == 0
    assert [p["output"] for p in again.predictions] == [p["output"] for p in first.predictions]


def test_answerer_render_attributes_speaker_and_filters_relationships(loop_env):
    cfg, _ = loop_env
    from loop.answerer import format_memory_block, render_query_prompt
    from loop.data import requester_relationships

    assert format_memory_block([{"record_id": "r1", "principal_id": "p_x", "text": "a\nb"}]) == \
        "Memory 1 (speaker=p_x): a b"
    assert format_memory_block([]) == "(none)"
    episode = {"entities": {"relationships": [{"type": "t", "holder_id": "p_x", "target_id": "p_y"},
                                              {"type": "t", "holder_id": "p_z", "target_id": "p_y"}]}}
    rels = requester_relationships(episode, {"principal_id": "p_x"})
    assert rels == [episode["entities"]["relationships"][0]]
    system, user = render_query_prompt({"asker": {"principal_id": "p_x", "role": "r"}, "query_text": "Q?",
                                        "evidence": [{"principal_id": "p_y", "text": "fact"}],
                                        "relationships": rels, "domain": "zephyr"}, cfg)
    assert "GLOBAL RULES" in system and "[REQUEST CONTEXT]" in user
    assert "Memory 1 (speaker=p_y): fact" in user and "holder_id=p_x" in user and "Q?" in user


def test_answerer_fallback_is_marked_degraded(loop_env):
    cfg, _ = loop_env
    from loop.answerer import render_answer
    from loop.llm_client import ChatResult

    class Dead:
        async def chat(self, **kw):
            return ChatResult(text="", model="m", route="vllm", error="HTTP 503: down")

    out, meta = asyncio.run(render_answer(Dead(), {"asker": {}, "query_text": "q", "action": "answer",
                                                    "evidence": [{"record_id": "r", "text": "body"}]}, cfg))
    assert meta["degraded"] and meta["error_kind"] == "no_response"
    assert out == {"action": "answer", "answer": "body", "answer_structured": {}, "used_record_ids": ["r"]}


def test_episode_timeout_does_not_stall_the_run(loop_env, dataset, seed_prompts, tmp_path, monkeypatch):
    """A hung child (EPISODE_TIMEOUT_S, DESIGN.md's evaluate.py TIMEOUT note)
    is recorded as a timeout error and does not block the other episode or
    the test. The stub sleeps well past a tiny timeout; the assertion is that
    `evaluate()` still returns -- with both episodes reported as failed --
    inside pytest's own wall clock, not that any particular time elapsed."""
    import dataclasses

    cfg, client = loop_env
    monkeypatch.setenv("SHM_STUB_SLEEP_S", "1.5")
    tiny_cfg = dataclasses.replace(cfg, episode_timeout_s=0.05)
    res = _evaluate(dataset, tiny_cfg, client, seed_prompts, tmp_path / "ev")
    eids = {e["episode_id"] for e in dataset.episodes}
    assert set(res.episode_errors) == eids
    assert all("timeout" in err for err in res.episode_errors.values())
    # Crashed/timed-out episodes still yield a scoreable row per checkpoint.
    assert len(res.predictions) == len(dataset.checkpoints)


def test_episode_timeout_counts_running_time_not_queueing(loop_env, dataset, seed_prompts, tmp_path, monkeypatch):
    """With one worker and two episodes each sleeping 0.4s, a 0.7s timeout
    must not fire: the second episode waits ~0.4s in the queue before it
    starts, and queueing is not the episode's time. The first full office
    run lost 13 of 17 episodes to exactly this (17 jobs, 3 workers, 3600s)."""
    import dataclasses

    cfg, client = loop_env
    monkeypatch.setenv("SHM_STUB_SLEEP_S", "0.4")
    one_worker = dataclasses.replace(cfg, episode_timeout_s=0.7, episode_workers=1)
    res = _evaluate(dataset, one_worker, client, seed_prompts, tmp_path / "ev")
    assert res.episode_errors == {}, res.episode_errors
    assert len(res.predictions) == len(dataset.checkpoints)


def test_real_agent_drops_in(loop_env, dataset, tmp_path):
    """The inner loop's GateMemAgent through the same evaluator, by import
    path. Skipped until memory_system.agent exists."""
    import importlib.util

    if importlib.util.find_spec("memory_system.agent") is None:
        pytest.skip("memory_system.agent not present yet")
    from loop import config as config_mod

    cfg, client = loop_env
    seed = config_mod.PACKAGE_ROOT / "memory_system" / "prompts"
    res = _evaluate(dataset, cfg, client, seed, tmp_path / "real",
                                agent_path="memory_system.agent:GateMemAgent")
    assert not res.episode_errors, res.episode_errors
    assert len(res.predictions) == len(dataset.checkpoints)
    assert res.traces and {t["component"] for t in res.traces} >= {"front_door", "processor", "retriever"}
    assert all(m["trace_source"] == "agent.trace_events" for m in res.report["episodes"].values())
