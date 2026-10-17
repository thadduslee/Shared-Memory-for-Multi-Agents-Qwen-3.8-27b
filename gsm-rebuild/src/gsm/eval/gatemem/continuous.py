"""Continuous mode: a whole GateMem domain as one memory, never reset between episodes.

GateMem resets the agent at the start of every episode. In continuous mode the episodes of a domain are
replayed in file order (which is chronological) into ONE episode, so memory written in an earlier episode is
still there in later ones. Turn ids get the episode's position as a prefix (``03.t017``) so they stay unique;
checkpoints keep their ids and point at the renamed turns, so predictions score exactly as before.

Principals are the union over the domain (a principal id that recurs is one person; when episodes disagree
on its role or name the first occurrence wins, and the conflict is counted). Relationships are the union.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from gsm.eval.gatemem.sanitize import MethodCheckpoint, MethodEpisode, MethodTurn


@dataclass(frozen=True)
class MergeReport:
    episodes: int
    turns: int
    principals: int
    principal_conflicts: tuple[str, ...]
    relationships: int


def turn_key(position: int, turn_id: str) -> str:
    return f"{position:02d}.{turn_id}"


def merge_domain(
    episodes: Sequence[MethodEpisode], checkpoints: Sequence[MethodCheckpoint]
) -> tuple[MethodEpisode, list[MethodCheckpoint], MergeReport]:
    if not episodes:
        raise ValueError("no episodes to merge")
    position = {e.episode_id: i for i, e in enumerate(episodes)}
    principals: dict[str, tuple[str, str, str]] = {}
    conflicts: set[str] = set()
    relationships: dict[str, dict[str, str]] = {}
    turns: list[MethodTurn] = []
    for i, ep in enumerate(episodes):
        for pid, role, name in ep.principals:
            if pid not in principals:
                principals[pid] = (pid, role, name)
            elif principals[pid][1:] != (role, name):
                conflicts.add(pid)
        for rel in ep.relationships:
            relationships.setdefault(json.dumps(rel, sort_keys=True), rel)
        turns.extend(t.model_copy(update={"turn_id": turn_key(i, t.turn_id)}) for t in ep.turns)
    domain = episodes[0].domain or "domain"
    merged = MethodEpisode(
        episode_id=f"{domain}_continuous",
        domain=episodes[0].domain,
        principals=tuple(principals.values()),
        relationships=tuple(relationships.values()),
        turns=tuple(turns),
    )
    ckpts = [
        c.model_copy(
            update={"episode_id": merged.episode_id, "as_of_turn_id": turn_key(position[c.episode_id], c.as_of_turn_id)}
        )
        for c in checkpoints
        if c.episode_id in position
    ]
    report = MergeReport(len(episodes), len(turns), len(principals), tuple(sorted(conflicts)), len(relationships))
    return merged, ckpts, report
