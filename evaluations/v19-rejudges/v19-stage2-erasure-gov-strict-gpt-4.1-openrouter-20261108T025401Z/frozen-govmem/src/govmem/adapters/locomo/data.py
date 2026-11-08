"""LoCoMo (locomo10.json) mapped onto the framework: two speakers who share everything, sessions with dates,
questions asked at the end of the conversation by a neutral questioner. Annotations (answers, evidence,
categories) never enter the memory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CATEGORIES = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop", 5: "adversarial"}
QUESTIONER = "questioner"


@dataclass
class Conversation:
    conv_id: str
    principals: list[dict[str, Any]]
    turns: list[dict[str, Any]]          # turn_id, timestamp, speaker_id, text
    questions: list[dict[str, Any]]      # qid, question (visible); gold kept separately


def _pid(name: str) -> str:
    return "spk_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def load(path: Path) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def to_conversation(sample: dict[str, Any], *, include_adversarial: bool = False) -> tuple[Conversation, dict]:
    conv = sample["conversation"]
    a, b = conv["speaker_a"], conv["speaker_b"]
    principals = [{"id": _pid(a), "role": "participant", "display_name": a},
                  {"id": _pid(b), "role": "participant", "display_name": b},
                  {"id": QUESTIONER, "role": "questioner", "display_name": ""}]
    sessions = sorted((int(k.split("_")[1]) for k in conv if re.fullmatch(r"session_\d+", k)))
    turns = []
    for n in sessions:
        when = conv.get(f"session_{n}_date_time", "")
        for t in conv[f"session_{n}"]:
            text = t.get("text", "")
            if t.get("blip_caption"):
                text = f"{text} [shares a photo: {t['blip_caption']}]".strip()
            turns.append({"turn_id": t["dia_id"], "timestamp": when, "speaker_id": _pid(t["speaker"]), "text": text})
    questions, gold = [], {}
    for i, q in enumerate(sample["qa"]):
        cat = int(q.get("category") or 0)
        if cat == 5 and not include_adversarial:
            continue
        qid = f"{sample['sample_id']}_q{i:04d}"
        questions.append({"qid": qid, "question": q["question"]})
        gold[qid] = {"answer": str(q.get("answer", q.get("adversarial_answer", ""))), "category": cat,
                     "category_name": CATEGORIES.get(cat, str(cat)), "evidence": list(q.get("evidence") or [])}
    return Conversation(sample["sample_id"], principals, turns, questions), gold


def full_transcript(conv: Conversation) -> list[dict[str, Any]]:
    """Every turn as an evidence item, for the full-context baseline."""
    names = {p["id"]: p["display_name"] for p in conv.principals}
    return [{"record_id": t["turn_id"], "principal_id": t["speaker_id"], "speaker_name": names.get(t["speaker_id"], ""),
             "text": t["text"], "timestamp": t["timestamp"]} for t in conv.turns]
