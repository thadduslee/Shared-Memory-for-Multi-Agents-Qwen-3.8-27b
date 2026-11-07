"""Answer and judge for LoCoMo. One answer prompt and one judge for govmem and the full-context baseline.

Scores: token F1 as in the LoCoMo paper (normalised tokens; multi-hop gold answers split on commas, each part
matched to the prediction and averaged), and an LLM judge accuracy ("J"), generous on wording and date format,
as reported by the A-Mem / Mem0 line of work. Stemming is omitted.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from collections import Counter
from typing import Any

from govmem.llm import ChatProtocol, ReplyCache, extract_json

ANSWER_SYSTEM = """You answer questions about a long conversation between two people, using only the memories given.
Each memory shows when it was said and who said it.
- Answer briefly: a few words, a name, a date, a number or a short phrase. No explanation.
- Resolve relative times ("yesterday", "last week", "next month") against the date of the memory that mentions
  them, and answer with the actual date or period.
- If several memories conflict, prefer the most recent one.
- If the memories do not contain the answer, answer "Not mentioned".
Reply with only the answer."""

JUDGE_SYSTEM = """You grade an answer to a question about a conversation against the gold answer.
Label it CORRECT if it states the same fact as the gold answer, even if worded differently, shorter, longer, or
with a different date format (for example "May 7th" for "7 May 2023").
Label it WRONG if it states a different fact, is less specific than the gold in a way that loses the fact (for
example only the month when the gold is a specific day), misses the point, or says the information is not
available when the gold answer exists.
Reply with one JSON object only: {"label": "CORRECT"} or {"label": "WRONG"}."""


def format_memories(items: list[dict[str, Any]], names: dict[str, str]) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        who = it.get("speaker_name") or names.get(it.get("principal_id", ""), it.get("principal_id", ""))
        lines.append(f"[{it.get('timestamp', '')}] {who}: {str(it.get('text', '')).replace(chr(10), ' ')}")
    return "\n".join(lines)


def _cached_chat(chat: ChatProtocol, cache: ReplyCache, tag: str, system: str, user: str,
                 want_json: bool, max_tokens: int | None = None) -> tuple[str, bool]:
    key = hashlib.sha256(f"{tag}\x00{system}\x00{user}".encode()).hexdigest()
    hit = cache.get(key)
    if hit is not None and (not want_json or extract_json(hit) is not None):
        return hit, True
    for _ in range(3):
        res = chat.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                        temperature=0.0, max_tokens=max_tokens, tag=tag)
        if res.ok and res.text.strip() and (not want_json or extract_json(res.text) is not None):
            cache.put(key, res.text)
            return res.text, False
    return "", False


def answer(chat: ChatProtocol, cache: ReplyCache, question: str, items: list[dict[str, Any]],
           names: dict[str, str]) -> tuple[str, dict[str, Any]]:
    user = f"MEMORIES\n{format_memories(items, names)}\n\nQUESTION: {question}"
    text, cached = _cached_chat(chat, cache, "locomo_answer", ANSWER_SYSTEM, user, want_json=False)
    return text.strip(), {"prompt_chars": len(user), "cached": cached, "degraded": not text}


def judge(chat: ChatProtocol, cache: ReplyCache, question: str, gold: str, pred: str) -> tuple[bool | None, bool]:
    user = f"QUESTION: {question}\nGOLD ANSWER: {gold}\nANSWER TO GRADE: {pred}"
    text, _ = _cached_chat(chat, cache, "locomo_judge", JUDGE_SYSTEM, user, want_json=True, max_tokens=200)
    obj = extract_json(text) if text else None
    if not obj:
        return None, True
    return str(obj.get("label", "")).upper().startswith("CORRECT"), False


# ---------------------------------------------------------------- F1 (LoCoMo-style, unstemmed)
def _norm(s: str) -> list[str]:
    s = str(s).lower().replace(",", " ")
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = re.sub(r"\b(a|an|the|and)\b", " ", s)
    return s.split()


def f1_single(pred: str, gold: str) -> float:
    p, g = _norm(pred), _norm(gold)
    common = Counter(p) & Counter(g)
    same = sum(common.values())
    if not p or not g or same == 0:
        return 0.0
    prec, rec = same / len(p), same / len(g)
    return 2 * prec * rec / (prec + rec)


def f1(pred: str, gold: str, category: int) -> float:
    if category == 1:
        parts = [x.strip() for x in str(gold).split(",") if x.strip()]
        preds = [x.strip() for x in str(pred).split(",") if x.strip()] or [pred]
        if parts:
            return sum(max(f1_single(pp, gp) for pp in preds) for gp in parts) / len(parts)
    if category == 3:
        gold = str(gold).split(";")[0]
    return f1_single(pred, gold)
