"""The benchmark-facing agent. THIS IS A STUB: build it. See TASK.md.

`reset` / `ingest` / `query` are the whole interface the benchmark drives. The
field wall (`_visible`) is part of the interface, not of any design: the agent
never reads annotation fields, and the stub keeps that true before a line of
the system exists.
"""

from __future__ import annotations

from typing import Any

#: Benchmark annotations. Never read by the agent; `_visible` strips them.
HIDDEN_FIELDS = frozenset(
    {"query_type", "attack_type", "expected_action", "judge_spec", "leak_targets",
     "gold_answer_structured", "gold_refusal_category", "policy_snapshot"}
)

ACTIONS = ("answer", "answer_redacted", "refuse", "no_memory")


def _visible(checkpoint: Any) -> dict[str, Any]:
    """Project a checkpoint down to the agent-visible fields."""
    if not isinstance(checkpoint, dict):
        checkpoint = {
            key: getattr(checkpoint, key)
            for key in ("checkpoint_id", "episode_id", "as_of_turn_id", "query_text")
            if hasattr(checkpoint, key)
        } | {
            "asker": {
                "principal_id": getattr(checkpoint, "asker_principal_id", ""),
                "role": getattr(checkpoint, "asker_role", ""),
            }
        }
    return {k: v for k, v in checkpoint.items() if k not in HIDDEN_FIELDS}


class GateMemAgent:
    """Not built yet. The first iteration replaces this class with a system."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = str(db_path)

    def reset(self, episode: dict[str, Any]) -> None:
        raise NotImplementedError("no memory system has been built yet -- see TASK.md")

    def ingest(self, turn: Any) -> None:
        raise NotImplementedError("no memory system has been built yet -- see TASK.md")

    def query(self, checkpoint: Any) -> dict[str, Any]:
        _visible(checkpoint)
        raise NotImplementedError("no memory system has been built yet -- see TASK.md")
