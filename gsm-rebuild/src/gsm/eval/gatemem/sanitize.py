"""Strip scoring-only annotations before anything reaches the method.

This is an *allowlist*: only the fields below survive, so any hidden field -
the documented ``query_type``, ``expected_action``, ``judge_spec``,
``attack_type``, ``leak_targets``, or anything added later - is dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

SCORING_ONLY_FIELDS = frozenset({"query_type", "expected_action", "judge_spec", "attack_type", "leak_targets"})
CHECKPOINT_ALLOWLIST = frozenset({"checkpoint_id", "episode_id", "as_of_turn_id", "asker", "query_text"})


class MethodCheckpoint(BaseModel):
    """Everything the method may know about a checkpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str
    episode_id: str
    as_of_turn_id: str
    asker_principal_id: str
    # The asker's authenticated role. Given to the baselines' answerer prompt only;
    # GSM's own access decisions use its role bindings.
    asker_role: str = ""
    query_text: str


class MethodTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str
    speaker_principal_id: str
    text: str
    timestamp: str | None = None
    turn_kind: str | None = None


class MethodEpisode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    domain: str
    principals: tuple[tuple[str, str, str], ...]  # (principal_id, role, display_name)
    relationships: tuple[dict[str, str], ...]
    turns: tuple[MethodTurn, ...]


def sanitize_checkpoint(raw: dict[str, Any]) -> MethodCheckpoint:
    kept = {k: raw[k] for k in CHECKPOINT_ALLOWLIST if k in raw}
    asker = kept.get("asker") or {}
    return MethodCheckpoint(
        checkpoint_id=str(kept["checkpoint_id"]),
        episode_id=str(kept["episode_id"]),
        as_of_turn_id=str(kept["as_of_turn_id"]),
        asker_principal_id=str(asker.get("principal_id", "")),
        asker_role=str(asker.get("role", "")),
        query_text=str(kept["query_text"]),
    )


def sanitize_episode(raw: dict[str, Any]) -> MethodEpisode:
    ents = raw.get("entities") or {}
    principals = tuple(
        (str(p["principal_id"]), str(p.get("role", "")), str(p.get("display_name", p["principal_id"])))
        for p in ents.get("principals", [])
    )
    rels = tuple(
        {str(k): str(v) for k, v in r.items() if isinstance(v, str | int)} for r in ents.get("relationships", [])
    )
    turns = []
    for t in raw.get("turns", []):
        speaker = t.get("speaker") or {}
        turns.append(
            MethodTurn(
                turn_id=str(t["turn_id"]),
                speaker_principal_id=str(speaker.get("principal_id", "")),
                text=str(t.get("text", "")),
                timestamp=t.get("timestamp"),
                turn_kind=t.get("turn_kind"),
            )
        )
    return MethodEpisode(
        episode_id=str(raw["episode_id"]),
        domain=str(raw.get("domain", "")),
        principals=principals,
        relationships=rels,
        turns=tuple(turns),
    )


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def load_for_method(data_dir: Path) -> tuple[list[MethodEpisode], list[MethodCheckpoint]]:
    """The only loader the method uses. Hidden fields never leave this function."""
    episodes = [sanitize_episode(e) for e in _jsonl(data_dir / "episodes.jsonl")]
    checkpoints = [sanitize_checkpoint(c) for c in _jsonl(data_dir / "checkpoints.jsonl")]
    return episodes, checkpoints
