"""Getting the deliverable JSON out of a model reply, and JSONL I/O.

WHY NOT `memory_system.ops.extract_json`. That one takes the OUTERMOST
`{...}` of the whole reply, which is right for a terse classifier and wrong for
the three callers here: the answerer reasons (thinking ON), and a reasoning
trace that leaks into `content` -- a vLLM without a reasoning parser does that
-- routinely holds a draft object; the reflector writes Markdown around its
block; the curator returns a LIST. So: drop any `<think>` span, prefer fenced
blocks (the LAST fenced block wins for the reflector, whose prose may quote an
example first), then balanced spans.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text or "")
    # An unterminated <think> (reply cut by max_tokens) leaves nothing usable
    # before it; keep what follows the last close tag if any.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text


def _balanced_spans(text: str, opener: str, closer: str) -> list[str]:
    """Top-level balanced `opener...closer` spans, string-literal aware."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == opener:
            if depth == 0:
                start = i
            depth += 1
        elif ch == closer and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start:i + 1])
    return spans


def _loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        # The one repair worth making: trailing commas, which every model emits.
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
        except (json.JSONDecodeError, ValueError):
            return None


def extract_json(text: str | None, *, want: type = dict, last: bool = False) -> Any:
    """The deliverable JSON value of type `want` (dict or list), or None."""
    if not text:
        return None
    body = strip_thinking(text)
    fenced = [m.group(1).strip() for m in _FENCE.finditer(body)]
    if last:
        fenced.reverse()
    for cand in fenced:
        parsed = _loads(cand)
        if isinstance(parsed, want):
            return parsed
    opener, closer = ("{", "}") if want is dict else ("[", "]")
    spans = _balanced_spans(body, opener, closer)
    if last:
        spans.reverse()
    for cand in spans:
        parsed = _loads(cand)
        if isinstance(parsed, want):
            return parsed
    return None


# ---------------------------------------------------------------- JSONL


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    p = Path(path)
    if not p.is_file():
        return rows
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_jsonl(path: Path | str, rows: Iterable[Any]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str) else json.dumps(row, sort_keys=True, default=str)) + "\n")
            n += 1
    return n


def append_jsonl(path: Path | str, row: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def write_json(path: Path | str, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def read_json(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
