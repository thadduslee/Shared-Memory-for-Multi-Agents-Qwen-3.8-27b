"""Continuous mode: a whole domain as one memory, never reset between episodes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsm.config import Settings
from gsm.db.database import Database
from gsm.eval.gatemem.adapter import run_gatemem
from gsm.eval.gatemem.continuous import merge_domain
from gsm.eval.gatemem.sanitize import load_for_method

from .test_qwen_interpreter import FIXTURES, FakeModel, _shown, interpreter

pytestmark = pytest.mark.gatemem


def _two_episodes(tmp_path: Path) -> Path:
    base = json.loads((FIXTURES / "episodes.jsonl").read_text())
    roles = {p["principal_id"]: p["role"] for p in base["entities"]["principals"]}

    def ep(eid: str, turns: list[tuple[str, str, str]]) -> dict:
        return {
            **base,
            "episode_id": eid,
            "turns": [
                {
                    "turn_id": tid,
                    "timestamp": f"2026-03-0{1 if eid == 'e1' else 2}T09:{i:02d}",
                    "speaker": {"principal_id": who, "role": roles[who]},
                    "turn_kind": "dialogue",
                    "text": text,
                }
                for i, (tid, who, text) in enumerate(turns)
            ],
        }

    data = tmp_path / "data"
    data.mkdir()
    eps = [
        ep("e1", [("t001", "pm_ava", "Orion staging deploys run on the blue cluster.")]),
        ep("e2", [("t001", "eng_ben", "Morning all.")]),
    ]  # same turn id in both episodes
    (data / "episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in eps))
    ck = {
        "checkpoint_id": "e2_ckpt",
        "episode_id": "e2",
        "as_of_turn_id": "t001",
        "query_type": "utility",
        "asker": {"principal_id": "eng_ben", "role": "engineer"},
        "query_text": "Which cluster does staging use?",
        "expected_action": "answer",
        "judge_spec": {},
        "leak_targets": [],
    }
    (data / "checkpoints.jsonl").write_text(json.dumps(ck) + "\n")
    return data


def test_merge_keeps_checkpoint_ids_and_makes_turn_ids_unique(tmp_path: Path) -> None:
    episodes, checkpoints = load_for_method(_two_episodes(tmp_path))
    merged, ckpts, report = merge_domain(episodes, checkpoints)
    assert [t.turn_id for t in merged.turns] == ["00.t001", "01.t001"]
    assert ckpts[0].checkpoint_id == "e2_ckpt" and ckpts[0].as_of_turn_id == "01.t001"
    assert report.episodes == 2 and report.principals == 5 and not report.principal_conflicts


@pytest.mark.parametrize("continuous", [True, False])
def test_memory_carries_across_episodes_only_in_continuous_mode(
    app_db: Database, settings: Settings, tmp_path: Path, continuous: bool
) -> None:
    fact = {
        "kind": "fact",
        "full_audience": ["G1"],
        "facts": [{"quote": "Orion staging deploys run on the blue cluster."}],
    }
    full = settings.model_copy(update={"full_context_max_items": 50})
    chatter = {"kind": "chatter", "facts": []}
    # continuous mode renames the turns 00.t001 / 01.t001; per episode both are t001 (e2's then gets
    # e1's label, whose quote it does not contain, so only the message itself is kept)
    fake = FakeModel(labels={"00.t001": fact, "01.t001": chatter, "t001": fact})
    out = run_gatemem(
        app_db,
        full,
        _two_episodes(tmp_path),
        tmp_path / "out",
        interpreter=interpreter(fake),
        repairs=frozenset(),
        continuous=continuous,
    )
    shown = _shown(out)["e2_ckpt"]
    assert ("blue cluster" in shown) is continuous  # reset per episode: e2 starts empty
    assert (out.summary["continuous"] is not None) is continuous


def test_a_restriction_on_more_than_fifty_principals_is_split() -> None:
    from gsm.domain.operations import RestrictAccess

    names = [f"p{i:03d}" for i in range(120)]
    batches = [tuple(names[i : i + 50]) for i in range(0, len(names), 50)]
    assert [len(b) for b in batches] == [50, 50, 20]
    for b in batches:  # each batch is a valid operation; the whole list is not
        RestrictAccess(memory_id="mem_x", principal_ids=b)
    with pytest.raises(ValueError):
        RestrictAccess(memory_id="mem_x", principal_ids=tuple(names))
