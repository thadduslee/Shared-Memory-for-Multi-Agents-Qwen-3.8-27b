"""A tiny stand-in for `memory_system.agent.GateMemAgent`, for the loop tests.

Same interface as DESIGN.md §2.6 (`reset`, `ingest`, `query`) plus
`trace_events()`, which is how the evaluator reads the trace. Behaviour is a
dict, not a database, and deliberately simple:

  * every turn becomes one record whose audience is its speaker;
  * a turn containing "share with <principal_id>" also grants that principal
    (a processor `grant` event);
  * a turn containing "forget" deletes the speaker's earlier records (a
    processor `delete` event) and stores nothing;
  * `query` releases every live record whose audience holds the asker (so,
    by default, exactly the records the asker spoke) and denies the rest.

Test hooks, all via environment (the stub runs in a spawned process):
  SHM_STUB_WIDEN_AT=<n>   when the four prompts in SHM_PROMPTS_DIR hold more
                          than n bullet lines in total, also release records
                          whose text names the asker -- so a curator edit
                          changes behaviour and the gate has something to see;
  SHM_STUB_SEEN_LOG=<p>   append everything the agent is handed to <p>, so a
                          test can prove no annotation ever reached it;
  SHM_STUB_TRY_OPEN=<p>   try to open <p> in reset (the field-wall test);
  SHM_STUB_SLEEP_S=<f>    sleep for <f> seconds in reset, before anything
                          else -- a hung child, for the per-episode timeout
                          test (loop.evaluate's EPISODE_TIMEOUT_S).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from contracts import HIDDEN_FIELDS, PROMPT_FILES, TraceEvent

_SHARE = re.compile(r"share with (\w+)", re.IGNORECASE)


def _visible(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _visible(v) for k, v in obj.items() if k not in HIDDEN_FIELDS}
    if isinstance(obj, list):
        return [_visible(v) for v in obj]
    return obj


class StubAgent:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self.records: list[dict[str, Any]] = []
        self._trace: list[TraceEvent] = []
        self._seq = 0
        self._ingested = 0
        prompts_dir = Path(os.environ.get("SHM_PROMPTS_DIR", ""))
        texts = [(prompts_dir / f).read_text(encoding="utf-8") if (prompts_dir / f).is_file() else ""
                 for f in sorted(PROMPT_FILES.values())]
        self.prompts_hash = hashlib.sha256("\0".join(texts).encode()).hexdigest()[:12]
        bullets = sum(1 for t in texts for line in t.splitlines() if line.lstrip().startswith("- "))
        widen_at = os.environ.get("SHM_STUB_WIDEN_AT")
        self.widen = widen_at is not None and bullets > int(widen_at)

    # ------------------------------------------------------------ helpers

    def _seen(self, kind: str, obj: Any) -> None:
        path = os.environ.get("SHM_STUB_SEEN_LOG")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": kind, "obj": obj}, default=str) + "\n")

    def _emit(self, **kw: Any) -> None:
        self._seq += 1
        self._trace.append(TraceEvent(seq=self._seq, **kw))

    def trace_events(self) -> list[TraceEvent]:
        return list(self._trace)

    # ------------------------------------------------------------ interface

    def reset(self, episode: dict[str, Any]) -> None:
        sleep_s = os.environ.get("SHM_STUB_SLEEP_S")
        if sleep_s:
            time.sleep(float(sleep_s))
        self._seen("reset", episode)
        target = os.environ.get("SHM_STUB_TRY_OPEN")
        if target:
            open(target, encoding="utf-8").close()
        episode = _visible(episode)
        self.records, self._trace, self._seq, self._ingested = [], [], 0, 0
        self.principals = {p["principal_id"]: p for p in (episode.get("entities") or {}).get("principals") or []}

    def ingest(self, turn: dict[str, Any]) -> None:
        self._seen("ingest", turn)
        self._ingested += 1
        speaker = turn["speaker"]["principal_id"]
        role = turn["speaker"].get("role", "")
        text = turn["text"]
        chunk_id = f"{turn['turn_id']}:0"
        if "forget" in text.lower():
            gone = [r["record_id"] for r in self.records if r["speaker"] == speaker and not r["deleted"]]
            for r in self.records:
                if r["record_id"] in gone:
                    r["deleted"] = True
            self._emit(component="front_door", turn_id=turn["turn_id"], op="add", outcome="applied",
                       chunk_id=chunk_id, op_args={"kind": "delete", "suggested_audience": [speaker]})
            self._emit(component="processor", turn_id=turn["turn_id"], op="delete", outcome="applied",
                       chunk_id=chunk_id, record_ids=gone, op_args={"record_ids": gone},
                       signal_kind="instruction")
            return
        rid = f"r{len(self.records) + 1}"
        audience = {speaker}
        self.records.append({"record_id": rid, "speaker": speaker, "role": role, "text": text,
                             "audience": audience, "deleted": False})
        self._emit(component="front_door", turn_id=turn["turn_id"], op="add", outcome="applied",
                   chunk_id=chunk_id, op_args={"kind": "fact", "suggested_audience": [speaker]})
        self._emit(component="processor", turn_id=turn["turn_id"], op="add", outcome="applied",
                   chunk_id=chunk_id, record_ids=[rid], op_args={"chunk_id": chunk_id, "audience": [speaker]})
        for m in _SHARE.finditer(text):
            who = m.group(1)
            audience.add(who)
            self._emit(component="processor", turn_id=turn["turn_id"], op="grant", outcome="applied",
                       chunk_id=chunk_id, record_ids=[rid],
                       op_args={"record_ids": [rid], "principal_ids": [who], "authority": speaker},
                       signal_kind="instruction", llm_called=True)

    def query(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        self._seen("query", checkpoint)
        leaked = sorted(set(checkpoint) & HIDDEN_FIELDS)
        if leaked:
            raise AssertionError(f"stub received hidden fields {leaked}")
        cp = _visible(checkpoint)
        asker = cp["asker"]["principal_id"]
        live = [r for r in self.records if not r["deleted"]]
        released = [r for r in live if asker in r["audience"] or (self.widen and asker in r["text"])]
        denied = [r for r in live if r not in released]
        action = "answer" if released else ("refuse" if denied else "no_memory")
        self._emit(component="retriever", turn_id=cp.get("as_of_turn_id", ""), op="query", outcome="applied",
                   checkpoint_id=cp["checkpoint_id"], record_ids=[r["record_id"] for r in released],
                   denied_ids=[r["record_id"] for r in denied], op_args={"action": action},
                   signal_kind="read_time:denials" if denied else "")
        return {
            "action": action,
            "answer": "",
            "used_record_ids": [r["record_id"] for r in released],
            "evidence": [{"record_id": r["record_id"], "principal_id": r["speaker"], "role": r["role"],
                          "text": r["text"]} for r in released],
            "debug": {"retrieval": {"n_allowed": len(released), "n_denied": len(denied)},
                      "memory_llm": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
                      "n_ingested": self._ingested, "prompts_hash": self.prompts_hash, "widen": self.widen},
        }
