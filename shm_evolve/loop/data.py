"""The one place that knows GateMem's on-disk format, and the field wall.

Ported from `Auto Research/gatemem_adapter.py`. Layout read from the data, not
inferred: `bench/data/<domain>/episodes.jsonl` rows are
`{episode_id, domain, entities{principals, relationships?}, turns[...]}` with
turns `{turn_id, timestamp, speaker{principal_id, role}, turn_kind, text}`;
`checkpoints.jsonl` rows are `{checkpoint_id, episode_id, as_of_turn_id,
asker, query_text}` plus the hidden annotations in `contracts.HIDDEN_FIELDS`.
`domain` lives on the EPISODE, so checkpoints are filtered by joining on it.
Some medical episodes have no `relationships` key at all; readers default it.

THE WALL. Annotated checkpoints exist only on the scoring side (this process).
The evaluator writes `checkpoints.stripped.jsonl` and that file is the only
checkpoint source an episode process reads (evaluate.py enforces it with an
audit hook). `strip_hidden_fields` is deep because a nested copy of a
checkpoint would otherwise smuggle labels through.

PROBE SELECTION is per EPISODE, not per checkpoint, because evaluation is one
pass per episode: a checkpoint costs nothing extra once its episode runs, and
an episode costs the same whether one or all of its checkpoints are asked.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import ACTIONS, HIDDEN_FIELDS, PredictionRow
from loop.jsonx import read_jsonl

log = logging.getLogger("loop.data")

VISIBLE_CHECKPOINT_FIELDS = frozenset({"checkpoint_id", "episode_id", "as_of_turn_id", "asker", "query_text"})
ANSWERING = frozenset({"answer", "answer_redacted"})


# ---------------------------------------------------------------- the wall


def strip_hidden_fields(record: Any) -> Any:
    """A NEW structure with every hidden annotation key removed, recursively.
    The input is never mutated, so the judge's copy stays intact."""
    if isinstance(record, dict):
        return {k: strip_hidden_fields(v) for k, v in record.items() if k not in HIDDEN_FIELDS}
    if isinstance(record, list):
        return [strip_hidden_fields(v) for v in record]
    if isinstance(record, tuple):
        return tuple(strip_hidden_fields(v) for v in record)
    return record


def assert_no_hidden_fields(record: Any, *, where: str = "record") -> None:
    """Raise if a hidden key survived. A leak here silently inflates every
    score, so it is an error, not a warning."""
    blob = json.dumps(record, default=str)
    for name in HIDDEN_FIELDS:
        if f'"{name}"' in blob:
            raise AssertionError(f"hidden annotation field {name!r} leaked into {where}")


def visible_checkpoint(cp: dict[str, Any]) -> dict[str, Any]:
    """The agent-visible half of a checkpoint: an allow-list, then the deep
    strip as a second line of defence (asker is a nested dict)."""
    return strip_hidden_fields({k: cp[k] for k in VISIBLE_CHECKPOINT_FIELDS if k in cp})


def annotation(cp: dict[str, Any]) -> dict[str, Any]:
    return {k: cp.get(k) for k in HIDDEN_FIELDS if k in cp}


# ---------------------------------------------------------------- loading


@dataclass
class Dataset:
    """One or more domains, annotated. Lives on the scoring side of the wall."""

    domains: list[str]
    episodes: list[dict[str, Any]]
    checkpoints: list[dict[str, Any]]
    _by_episode: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_episode = {}
        for cp in self.checkpoints:
            self._by_episode.setdefault(cp["episode_id"], []).append(cp)

    @property
    def episodes_by_id(self) -> dict[str, dict[str, Any]]:
        return {ep["episode_id"]: ep for ep in self.episodes}

    @property
    def checkpoints_by_id(self) -> dict[str, dict[str, Any]]:
        return {cp["checkpoint_id"]: cp for cp in self.checkpoints}

    def checkpoints_for(self, episode_id: str) -> list[dict[str, Any]]:
        return list(self._by_episode.get(episode_id, []))

    def annotations_by_id(self) -> dict[str, dict[str, Any]]:
        return {cp["checkpoint_id"]: annotation(cp) for cp in self.checkpoints}

    def visible_by_id(self) -> dict[str, dict[str, Any]]:
        return {cp["checkpoint_id"]: visible_checkpoint(cp) for cp in self.checkpoints}

    def subset(self, episode_ids: list[str]) -> Dataset:
        keep = set(episode_ids)
        return Dataset(
            domains=list(self.domains),
            episodes=[ep for ep in self.episodes if ep["episode_id"] in keep],
            checkpoints=[cp for cp in self.checkpoints if cp["episode_id"] in keep],
        )


def load_domain(data_root: Path, domain: str) -> Dataset:
    """Load one domain. The filter is on the episode's own `domain` field
    (when present) and propagated to checkpoints by episode_id; a checkpoint
    whose episode is missing is dropped, since its domain cannot be verified."""
    d = Path(data_root) / domain
    episodes_path, checkpoints_path = d / "episodes.jsonl", d / "checkpoints.jsonl"
    if not episodes_path.is_file() or not checkpoints_path.is_file():
        raise FileNotFoundError(f"no GateMem data for domain {domain!r} under {d}")
    episodes = [ep for ep in read_jsonl(episodes_path) if ep.get("domain", domain) == domain]
    ids = {ep["episode_id"] for ep in episodes}
    checkpoints = [cp for cp in read_jsonl(checkpoints_path) if cp.get("episode_id") in ids]
    if not checkpoints:
        raise ValueError(f"no checkpoints survived the domain filter for {domain!r} under {d}")
    for ep in episodes:
        ep.setdefault("domain", domain)
        ep.setdefault("entities", {}).setdefault("relationships", [])
    return Dataset(domains=[domain], episodes=episodes, checkpoints=checkpoints)


def list_domains(data_root: Path) -> list[str]:
    root = Path(data_root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "episodes.jsonl").is_file())


def load_dataset(data_root: Path, domains: list[str]) -> Dataset:
    parts = [load_domain(data_root, d) for d in domains]
    return Dataset(
        domains=list(domains),
        episodes=[ep for p in parts for ep in p.episodes],
        checkpoints=[cp for p in parts for cp in p.checkpoints],
    )


# ---------------------------------------------------------------- selection


def select_episodes(episodes: list[dict[str, Any]], n: int | None, seed: int) -> list[str]:
    """`n` episode ids per domain, seeded and order-independent (sorted first),
    or all of them when n is None. Used for `--episodes`."""
    by_domain: dict[str, list[str]] = {}
    for ep in sorted(episodes, key=lambda e: e["episode_id"]):
        by_domain.setdefault(str(ep.get("domain") or ""), []).append(ep["episode_id"])
    out: list[str] = []
    for dom in sorted(by_domain):
        ids = by_domain[dom]
        if n is None or n >= len(ids):
            out.extend(ids)
        else:
            out.extend(sorted(random.Random(f"{seed}:{dom}").sample(ids, n)))
    return out


def select_probe(episodes: list[dict[str, Any]], per_domain: int, seed: int) -> list[str]:
    """The fixed probe: `per_domain` episodes per domain, seeded. Fixed for the
    whole run -- a probe that changes between champion and candidate is not a
    paired comparison. The seed string includes the domain so adding a domain
    does not reshuffle the others."""
    return select_episodes(episodes, per_domain, seed)


def select_split(episodes: list[dict[str, Any]], heldout_per_domain: int, seed: int) -> tuple[list[str], list[str]]:
    """(train episode ids, held-out episode ids), `heldout_per_domain` held
    out from every domain, seeded per domain so adding a domain does not
    reshuffle the others. The held-out episodes are scored and never
    attributed, probed or promoted: the loop evolves on train only, and the
    held-out number is the one to put next to the baselines."""
    by_domain: dict[str, list[str]] = {}
    for ep in sorted(episodes, key=lambda e: e["episode_id"]):
        by_domain.setdefault(str(ep.get("domain") or ""), []).append(ep["episode_id"])
    train: list[str] = []
    held: list[str] = []
    for dom in sorted(by_domain):
        ids = by_domain[dom]
        n = max(0, min(heldout_per_domain, len(ids) - 1))
        chosen = set(random.Random(f"{seed}:heldout:{dom}").sample(ids, n)) if n else set()
        held.extend(i for i in ids if i in chosen)
        train.extend(i for i in ids if i not in chosen)
    return train, held


def select_dev_slice(checkpoints: list[dict[str, Any]], *, n: int, seed: int) -> list[dict[str, Any]]:
    """A stable, stratified `n`-checkpoint slice (ported verbatim in intent).

    Deterministic (sorted by id before sampling) and stratified by query_type
    so a slice never has zero safety checkpoints (F undefined). Reading
    query_type here is legitimate: this runs on the scoring side.
    """
    ordered = sorted(checkpoints, key=lambda cp: cp["checkpoint_id"])
    if n >= len(ordered):
        return ordered
    buckets: dict[str, list[dict[str, Any]]] = {}
    for cp in ordered:
        buckets.setdefault(str(cp.get("query_type") or "unknown"), []).append(cp)
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        quota = max(1, round(n * len(bucket) / len(ordered)))
        selected.extend(rng.sample(bucket, min(quota, len(bucket))))
    selected.sort(key=lambda cp: cp["checkpoint_id"])
    if len(selected) > n:
        selected = selected[:n]
    elif len(selected) < n:
        chosen = {cp["checkpoint_id"] for cp in selected}
        for cp in ordered:
            if len(selected) >= n:
                break
            if cp["checkpoint_id"] not in chosen:
                selected.append(cp)
        selected.sort(key=lambda cp: cp["checkpoint_id"])
    return selected


# ---------------------------------------------------------------- episode ordering


_NUM = re.compile(r"(\d+)")


def natural_key(s: str) -> tuple[Any, ...]:
    """t9 < t10. Turn ids are opaque strings; this only decides whether an
    unmatched as_of falls before the first turn."""
    return tuple(int(p) if p.isdigit() else p for p in _NUM.split(str(s)))


def place_checkpoints(turn_ids: list[str], checkpoints: list[dict[str, Any]]
                      ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Where each checkpoint is asked in a one-pass replay.

    Returns (before_any_turn, after_turn[turn_id], unplaced_after_last).
    A checkpoint whose as_of matches a turn is asked right after that turn is
    ingested -- including the last turn. One whose as_of matches no turn and
    sorts before the first turn is asked after `reset` with nothing ingested.
    Anything else is asked after the last turn and reported as unplaced: the
    data says nothing better, and dropping it would break the paired gate.
    File order is preserved within a turn.
    """
    known = set(turn_ids)
    before: list[dict[str, Any]] = []
    after: dict[str, list[dict[str, Any]]] = {}
    unplaced: list[dict[str, Any]] = []
    first = natural_key(turn_ids[0]) if turn_ids else None
    for cp in checkpoints:
        as_of = str(cp.get("as_of_turn_id") or "")
        if as_of in known:
            after.setdefault(as_of, []).append(cp)
        elif first is None or (as_of and natural_key(as_of) < first):
            before.append(cp)
        else:
            unplaced.append(cp)
    return before, after, unplaced


def requester_relationships(episode: dict[str, Any], asker: dict[str, Any]) -> list[dict[str, Any]]:
    """Relationship rows naming the requester in any `*_id` field. Mirrors
    `bench/agents/base.py::_relationship_mentions_principal`: the answerer is
    shown only the requester's own relationships, never the whole graph."""
    principal = str((asker or {}).get("principal_id") or "")
    if not principal:
        return []
    out = []
    for rel in ((episode or {}).get("entities") or {}).get("relationships") or []:
        if isinstance(rel, dict) and any(
            str(k).lower().endswith("_id") and v == principal for k, v in rel.items()
        ):
            out.append(rel)
    return out


# ---------------------------------------------------------------- prediction rows


def make_prediction_row(
    *, checkpoint_id: str, episode_id: str, as_of_turn_id: str, asker: dict[str, str],
    action: str, answer: str, answer_structured: dict[str, Any] | None = None,
    used_record_ids: list[str] | None = None, debug: dict[str, Any] | None = None,
    trace_seq: list[int] | None = None, render: dict[str, Any] | None = None,
) -> PredictionRow:
    """One predictions.jsonl row with the NESTED `output` that
    bench/scripts/score_predictions.py accepts without normalising. An
    unknown action is coerced to `refuse`: an invalid string would be scored
    as a malformed row, and a broken build scored as "no answer" is the more
    honest of the two."""
    if action not in ACTIONS:
        log.warning("invalid action %r for %s -- coercing to 'refuse'", action, checkpoint_id)
        action = "refuse"
    output: dict[str, Any] = {
        "action": action,
        "answer": answer or "",
        "answer_structured": answer_structured or {},
        "used_record_ids": list(used_record_ids or []),
    }
    if debug:
        output["debug"] = debug
    return PredictionRow(
        checkpoint_id=checkpoint_id, episode_id=episode_id, as_of_turn_id=as_of_turn_id,
        asker=dict(asker or {}), output=output, trace_seq=list(trace_seq or []), render=dict(render or {}),
    )
