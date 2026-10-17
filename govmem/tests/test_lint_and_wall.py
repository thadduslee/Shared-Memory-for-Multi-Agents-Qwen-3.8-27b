"""I-14 (no dataset vocabulary in code or prompts) and I-15 (annotation fields never reach the memory)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import FakeChat, settings

from govmem.adapters.gatemem.agent import GateMemAgent
from govmem.config import REPO_ROOT

SRC = Path(__file__).resolve().parents[1] / "src" / "govmem"
DATA = REPO_ROOT / "bench" / "data"
# Benchmark-specific by design: adapters map one benchmark onto the framework.
EXEMPT_DIRS = ("adapters",)
# Ordinary English words that also happen to be role/type labels in the data, used in their ordinary sense.
ALLOWED = {"agent", "member", "owner", "guest", "contractor", "delegate", "manager", "support", "staff", "service",
           "grant", "shared", "operations"}  # generic system terms that also appear as names or labels in the data


def _vocabulary() -> set[str]:
    vocab: set[str] = set()
    for f in DATA.glob("*/episodes.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            ep = json.loads(line)
            ents = ep.get("entities") or {}
            for p in ents.get("principals") or []:
                vocab.add(p["principal_id"])
                vocab.add(p.get("role") or "")
                for tok in re.findall(r"[A-Za-z]{4,}", re.sub(r"\([^)]*\)", " ", p.get("display_name") or "")):
                    vocab.add(tok)
            for r in ents.get("relationships") or []:
                vocab.add(r.get("type") or "")
                for k, v in r.items():
                    if k.endswith("_id") and isinstance(v, str):
                        vocab.add(v)
    return {v.lower() for v in vocab if v and len(v) >= 4} - ALLOWED


def _files() -> list[Path]:
    out = []
    for p in SRC.rglob("*"):
        if p.suffix in (".py", ".md") and not any(part in EXEMPT_DIRS for part in p.relative_to(SRC).parts):
            out.append(p)
    return out


@pytest.mark.skipif(not DATA.exists(), reason="benchmark data not present")
def test_no_dataset_vocabulary_in_code_or_prompts():
    vocab = _vocabulary()
    hits = []
    for f in _files():
        text = f.read_text(encoding="utf-8").lower()
        words = set(re.findall(r"[a-z][a-z0-9_]+", text))
        for w in sorted(vocab & words):
            hits.append(f"{f.relative_to(SRC)}: {w}")
    assert not hits, "dataset vocabulary found:\n" + "\n".join(hits)


def test_annotation_fields_never_reach_any_component(tmp_path):
    canary = {k: f"CANARY-{k}-9f3a" for k in ("query_type", "expected_action", "attack_type")}
    episode = {
        "episode_id": "ep_x", "domain": "synthetic",
        "entities": {"principals": [{"principal_id": "p_one", "role": "r1", "display_name": "Ona Vale"},
                                    {"principal_id": "p_two", "role": "r2", "display_name": "Tam Rook"}],
                     "relationships": [{"type": "rt", "a_id": "p_one", "b_id": "p_two", "secret": "x"}]},
        "turns": [{"turn_id": "t1", "timestamp": "", "turn_kind": "dialogue",
                   "speaker": {"principal_id": "p_one", "role": "r1"}, "text": "The gate code is 7781.",
                   "hidden_note": "CANARY-turn-9f3a"}],
        "judge_spec": {"include": ["CANARY-judge-9f3a"]},
    }
    cp = {"checkpoint_id": "c1", "episode_id": "ep_x", "as_of_turn_id": "t1",
          "asker": {"principal_id": "p_two", "role": "r2"}, "query_text": "gate code?",
          "judge_spec": {"include": ["CANARY-judge-9f3a"]}, "leak_targets": ["CANARY-leak-9f3a"], **canary}
    chat = FakeChat()
    agent = GateMemAgent(str(tmp_path / "m.db"), settings(), chat=chat)
    agent.reset(episode)
    agent.ingest(episode["turns"][0])
    out = agent.query(cp)
    seen = json.dumps(chat.seen) + json.dumps(out)
    agent.mem.close()  # type: ignore[union-attr]
    blob = b"".join(f.read_bytes() for f in tmp_path.iterdir())
    for marker in ("CANARY-",):
        assert marker not in seen
        assert marker.encode() not in blob
