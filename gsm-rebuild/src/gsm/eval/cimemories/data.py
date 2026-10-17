"""CIMemories data: what a method may see, and the labelled file only scoring reads.

A CIMemories profile is one user's persistent memory (~147 memory statements) plus 49 task contexts
(recipient, purpose). Per context, the gold labels say which statements are *necessary* for the purpose
and which the user would keep *private* from that recipient.

Method side (``load_for_method``) is an **allowlist**: each profile's memory statements under opaque ids
(``m000``, ``m001``, ...) and each context's ``recipient`` and ``task``. Attribute keys
(``bankruptcy_finance_filing_fees``), raw values, ``information_domain`` / ``event`` and every
``labels_*`` field stay behind; the official task prompt shows the model statements only, too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

REPO_ROOT = Path(__file__).resolve().parents[5]
UPSTREAM = REPO_ROOT / "third_party" / "CIMemories"
DEFAULT_DATA = UPSTREAM / "data" / "data_openai_gpt-oss-120b.json"
DEFAULT_PROMPTS = UPSTREAM / "eval" / "prompts.yaml"
DEFAULT_METRICS = UPSTREAM / "eval" / "metrics.py"


class MethodMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: str
    statement: str


class MethodContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    context_id: str
    recipient: str
    task: str


class MethodProfile(BaseModel):
    """Everything a method may know about one user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    memories: tuple[MethodMemory, ...]
    contexts: tuple[MethodContext, ...]


def memory_id(index: int) -> str:
    return f"m{index:03d}"


def context_id(profile_index: int, context_index: int) -> str:
    return f"p{profile_index:02d}c{context_index:02d}"


def profile_id(profile_index: int) -> str:
    return f"p{profile_index:02d}"


def load_raw(path: Path = DEFAULT_DATA) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("profiles", [data])


def statements(raw_profile: dict[str, Any]) -> list[tuple[str, str]]:
    """(attribute key, the sentence upstream shows for it), in file order.

    Mirrors upstream ``memory_map`` + ``mem_blob``: bio sentences are registered first, so an attribute
    whose key is also a bio key (``name``, ``age``, ``sex``, ``address``) is shown as the bio sentence.
    """
    mem: dict[str, list[str]] = {}
    bio = raw_profile.get("bio") or {}
    if "name" in bio:
        mem.setdefault("name", []).append(f"{bio['name']} is the person's name.")
    if "age" in bio:
        mem.setdefault("age", []).append(f"The person is {bio['age']} years old.")
    if "sex" in bio:
        mem.setdefault("sex", []).append(f"The person's sex is {bio['sex']}.")
    if "address" in bio:
        mem.setdefault("address", []).append(f"The person's address is {bio['address']}.")
    for k, v in bio.items():
        if k not in {"name", "age", "sex", "address"}:
            mem.setdefault(k, []).append(f"{k}: {v}")
    attrs = raw_profile.get("information_attributes") or {}
    for key, obj in attrs.items():
        if isinstance(obj, dict):
            stmt, val = obj.get("memory_statement"), obj.get("value")
            if isinstance(stmt, str) and stmt.strip():
                mem.setdefault(key, []).append(stmt.strip())
            elif val is not None:
                mem.setdefault(key, []).append(f"{key}: {val}")
        else:
            mem.setdefault(key, []).append(f"{key}: {obj}")
    return [(key, mem[key][0]) for key in attrs if mem.get(key)]


def sanitize_profile(index: int, raw: dict[str, Any]) -> MethodProfile:
    return MethodProfile(
        profile_id=profile_id(index),
        memories=tuple(MethodMemory(memory_id=memory_id(i), statement=s) for i, (_, s) in enumerate(statements(raw))),
        contexts=tuple(
            MethodContext(
                context_id=context_id(index, j), recipient=str(c.get("recipient", "")), task=str(c.get("task", ""))
            )
            for j, c in enumerate(raw.get("contexts") or [])
        ),
    )


def load_for_method(path: Path = DEFAULT_DATA, *, max_profiles: int | None = None) -> list[MethodProfile]:
    """The only loader a method uses. Labels and attribute keys never leave this function."""
    raw = load_raw(path)[: max_profiles if max_profiles is not None else None]
    return [sanitize_profile(i, p) for i, p in enumerate(raw)]
