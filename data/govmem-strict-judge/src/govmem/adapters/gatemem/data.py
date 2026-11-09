"""GateMem data with the annotation wall: the memory only ever sees allowlisted fields (SPEC §10.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CHECKPOINT_VISIBLE = ("checkpoint_id", "episode_id", "as_of_turn_id", "query_text")
ASKER_VISIBLE = ("principal_id", "role")
TURN_VISIBLE = ("turn_id", "timestamp", "turn_kind", "text")
ANNOTATION_FIELDS = ("query_type", "expected_action", "judge_spec", "leak_targets", "attack_type")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def visible_checkpoint(cp: dict[str, Any]) -> dict[str, Any]:
    out = {k: cp[k] for k in CHECKPOINT_VISIBLE if k in cp}
    out["asker"] = {k: (cp.get("asker") or {}).get(k) for k in ASKER_VISIBLE}
    return out


def visible_turn(t: dict[str, Any]) -> dict[str, Any]:
    out = {k: t.get(k) for k in TURN_VISIBLE}
    sp = t.get("speaker") or {}
    out["speaker"] = {"principal_id": sp.get("principal_id"), "role": sp.get("role")}
    return out


def visible_episode(ep: dict[str, Any]) -> dict[str, Any]:
    ents = ep.get("entities") or {}
    return {"episode_id": ep["episode_id"], "domain": ep.get("domain", ""),
            "entities": {"principals": list(ents.get("principals") or []),
                         "relationships": list(ents.get("relationships") or [])},
            "turns": [visible_turn(t) for t in ep.get("turns") or []]}


def annotation(cp: dict[str, Any]) -> dict[str, Any]:
    return {k: cp.get(k) for k in ANNOTATION_FIELDS}


class GateMemData:
    def __init__(self, data_root: Path, domain: str, continuous: bool = False) -> None:
        self.domain = domain
        d = Path(data_root) / domain
        self.episodes = read_jsonl(d / "episodes.jsonl")
        self.checkpoints = read_jsonl(d / "checkpoints.jsonl")
        if continuous:
            self.episodes, self.checkpoints = merge_continuous(self.episodes, self.checkpoints, domain)


    def select(self, n: int | None = None, ids: list[str] | None = None) -> list[dict[str, Any]]:
        eps = self.episodes
        if ids:
            eps = [e for e in eps if e["episode_id"] in set(ids)]
        if n:
            eps = eps[:n]
        return eps

    def checkpoints_for(self, episode_id: str) -> list[dict[str, Any]]:
        return [c for c in self.checkpoints if c["episode_id"] == episode_id]


def requester_relationships(episode: dict[str, Any], asker: dict[str, Any]) -> list[dict[str, Any]]:
    """Relationship rows naming the requester in any *_id field (mirrors the benchmark's own filter)."""
    pid = str((asker or {}).get("principal_id") or "")
    out = []
    for rel in ((episode or {}).get("entities") or {}).get("relationships") or []:
        if isinstance(rel, dict) and any(str(k).lower().endswith("_id") and v == pid for k, v in rel.items()):
            out.append(rel)
    return out


def merge_continuous(episodes: list[dict[str, Any]], checkpoints: list[dict[str, Any]],
                     domain: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One conversation per domain, never reset: episodes in file order, turn ids prefixed by episode position,
    principals and relationships unioned (a recurring id is one principal; first occurrence kept)."""
    eid = f"continuous_{domain}"
    principals: dict[str, dict[str, Any]] = {}
    rels: list[dict[str, Any]] = []
    seen_rel: set[str] = set()
    turns: list[dict[str, Any]] = []
    prefix: dict[str, str] = {}
    for i, ep in enumerate(episodes, 1):
        pre = f"e{i:02d}:"
        prefix[ep["episode_id"]] = pre
        ents = ep.get("entities") or {}
        for p in ents.get("principals") or []:
            principals.setdefault(p["principal_id"], p)
        for r in ents.get("relationships") or []:
            k = json.dumps(r, sort_keys=True)
            if k not in seen_rel:
                seen_rel.add(k)
                rels.append(r)
        for t in ep.get("turns") or []:
            turns.append({**t, "turn_id": pre + t["turn_id"]})
    merged = {"episode_id": eid, "domain": episodes[0].get("domain", domain) if episodes else domain,
              "entities": {"principals": list(principals.values()), "relationships": rels}, "turns": turns}
    cps = [{**c, "episode_id": eid, "as_of_turn_id": prefix[c["episode_id"]] + c["as_of_turn_id"],
            "source_episode_id": c["episode_id"]} for c in checkpoints if c["episode_id"] in prefix]
    return [merged], cps
