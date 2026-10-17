"""run.py end to end with the stub agent and the fakes: two iterations, a
release and a rejection, history rows, resume from the run dir."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.loop.conftest import DOMAIN


def _run(run_dir: Path, iterations: int, *extra: str) -> Path:
    from loop.run import _parse_args, run_loop

    args = _parse_args(["--domain", DOMAIN, "--iterations", str(iterations), "--fake-llm",
                        "--run-dir", str(run_dir), *extra])
    return asyncio.run(run_loop(args))


def _history(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "history.jsonl").read_text().splitlines()]


def test_two_iterations_record_release_and_rejection(loop_env, tmp_path):
    run_dir = _run(tmp_path / "run", 2)
    hist = _history(run_dir)
    assert [r["iteration"] for r in hist] == [1, 2]
    # iteration 1: the curator's bullet widens the stub; the gate sees one win and no loss
    assert hist[0]["accepted"] and (hist[0]["wins"], hist[0]["losses"]) == (1, 0)
    assert hist[0]["head_before"] == 0 and hist[0]["head_after"] == 1
    assert hist[0]["candidate_full"]["utility"] > hist[0]["champion"]["utility"]
    # iteration 2: same behaviour, nothing to win -> rejected, champion unchanged
    assert not hist[1]["accepted"] and hist[1]["head_after"] == 1
    assert (run_dir / "releases" / "HEAD").read_text().strip() == "1"
    meta = json.loads((run_dir / "releases" / "1" / "meta.json").read_text())
    assert meta["parent"] == 0 and meta["component_changed"] == hist[0]["component"] and meta["accepted"]
    delta = json.loads((run_dir / "releases" / "1" / "delta.json").read_text())
    assert delta["component"] == hist[0]["component"] and len(delta["edits"]) == 1
    changed = (run_dir / "releases" / "1" / "prompts" / f"{hist[0]['component']}.md").read_text()
    assert delta["edits"][0]["text"] in changed
    rejected = json.loads((run_dir / "rejected" / "2" / "meta.json").read_text())
    assert rejected["accepted"] is False and rejected["parent"] == 1
    # the champion's evaluation is cached per release: iteration 2 did not re-run it
    assert hist[1]["champion_eval"] == "iter_1/candidate_full"
    assert not (run_dir / "iter_2" / "champion_eval").exists()
    # every stage left its artefacts
    it1 = run_dir / "iter_1"
    for rel in ("champion_eval/predictions.jsonl", "champion_eval/traces.jsonl", "champion_eval/eval_report.json",
                "champion_eval/verdicts.jsonl", "champion_eval/scores.json", "champion_eval/failures.jsonl",
                "champion_eval/attribution.json", "reflector/diagnosis.json", "curator/curation.json",
                "candidate_probe/verdicts.jsonl", "gate.json"):
        assert (it1 / rel).is_file(), rel


def test_reflector_is_shown_rejections(loop_env, tmp_path):
    run_dir = _run(tmp_path / "run", 3)
    hist = _history(run_dir)
    assert len(hist) == 3
    prompt3 = (run_dir / "iter_3" / "reflector" / "prompt.md").read_text()
    assert "## Rejected proposals" in prompt3 and hist[1]["mechanism"][:60] in prompt3
    # the fake reflector does not re-propose a rejected (component, mechanism)
    assert (hist[2]["component"], hist[2]["mechanism"]) != (hist[1]["component"], hist[1]["mechanism"])


def test_resume_reads_head_and_continues(loop_env, tmp_path):
    run_dir = _run(tmp_path / "run", 1)
    assert len(_history(run_dir)) == 1
    # a crashed second attempt left a partial iteration dir behind
    (run_dir / "iter_2").mkdir()
    (run_dir / "iter_2" / "junk").write_text("partial")
    _run(run_dir, 2)
    hist = _history(run_dir)
    assert [r["iteration"] for r in hist] == [1, 2]
    assert hist[1]["head_before"] == hist[0]["head_after"]       # resumed from the HEAD iteration 1 left
    assert any(p.name.startswith("iter_2.aborted") for p in run_dir.iterdir())
    _run(run_dir, 2)                              # already complete: nothing more runs
    assert len(_history(run_dir)) == 2


def test_probe_only_mode(loop_env, tmp_path):
    run_dir = _run(tmp_path / "run", 1, "--probe-only", "--episodes", "1")
    hist = _history(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert len(run_meta["eval_episodes"]) == 1 and run_meta["probe_only"]
    assert len(hist) == 1 and not (run_dir / "iter_1" / "candidate_full").exists()


def test_cli_main_runs(loop_env, tmp_path, capsys):
    from loop.run import main

    assert main(["--domain", DOMAIN, "--iterations", "1", "--fake-llm", "--run-dir", str(tmp_path / "cli")]) == 0
    assert len(_history(tmp_path / "cli")) == 1


@pytest.mark.parametrize("missing", ["retriever.md"])
def test_missing_seed_prompt_is_empty_not_fatal(loop_env, tmp_path, seed_prompts, missing):
    (seed_prompts / missing).unlink()
    run_dir = _run(tmp_path / "run", 1)
    assert (run_dir / "releases" / "0" / "prompts" / missing).read_text() == ""
