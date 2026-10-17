"""Loading a component's prompt and asking the memory model with it.

One evolvable prompt file per component (contracts.PROMPT_FILES). The outer
loop evaluates candidate prompts by pointing the memory at another directory
(`GateMemAgent(prompts_dir=...)` or `SHM_PROMPTS_DIR`); a file missing there
falls back to the seed in `memory_system/prompts/`, so a release that changed
one component need only carry that one file.

The prompt is the SYSTEM message. The user message is the component's input
as one JSON object (with `"component"` repeated inside it), so the reply can
be checked against an exact contract and the deterministic fake can tell who
is asking without parsing prose.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from contracts import PROMPT_FILES, Component
from memory_system import llm
from memory_system.ops import extract_json

SEED_DIR = Path(__file__).resolve().parent / "prompts"

#: Completion caps per component: these are classifiers, not writers.
MAX_TOKENS = {
    Component.FRONT_DOOR: 600,
    Component.PROCESSOR: 700,
    Component.LIBRARIAN: 250,
    Component.RETRIEVER: 350,
}


def thinking_for(component: Component, payload: dict[str, Any]) -> bool:
    """Whether this call runs in the model's reasoning mode.

    MEMORY_LLM_THINKING_COMPONENTS names the calls, comma-separated, as
    `<component>` or `<component>:<stage>` (the retriever's two stages are
    `search` and `select`): e.g. "librarian,retriever:select". Reasoning is
    bought only where a decision is hard and the calls are few -- structure
    ops and record selection -- not for the per-turn classifiers, where it
    would multiply an episode's cost several-fold.
    """
    names = {n.strip() for n in os.environ.get("MEMORY_LLM_THINKING_COMPONENTS", "").split(",") if n.strip()}
    if not names:
        return False
    stage = str(payload.get("stage") or "")
    return component.value in names or (bool(stage) and f"{component.value}:{stage}" in names)


def THINKING_MAX_TOKENS(component: Component | None = None) -> int:  # noqa: N802 - reads like the constant it replaces
    """Completion cap for a reasoning call: the reasoning shares it with the
    answer, so it is far above the classifier caps. A per-component cap
    (MEMORY_LLM_THINKING_MAX_TOKENS_<COMPONENT>) overrides the shared one:
    the librarian's median reply is ~800 tokens and its tail is pure cost."""
    keys = ([f"MEMORY_LLM_THINKING_MAX_TOKENS_{component.value.upper()}"] if component else []) + \
        ["MEMORY_LLM_THINKING_MAX_TOKENS"]
    for key in keys:
        raw = os.environ.get(key, "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return 4096


def load_prompt(component: str, prompts_dir: str | os.PathLike[str] | None = None) -> str:
    comp = Component(component)
    for base in (prompts_dir, os.environ.get("SHM_PROMPTS_DIR") or None, SEED_DIR):
        if base:
            path = Path(base) / PROMPT_FILES[comp]
            if path.is_file():
                return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"no prompt for {comp.value}")


def ask(component: str, payload: dict[str, Any], *, prompts_dir: str | None = None) -> tuple[Any, bool]:
    """(decoded JSON reply or None, whether a model answer was obtained --
    fresh or from llm.py's cache). Disabled, over budget and failed calls are
    (None, False); an answer that does not parse is (None, True). Either way
    the caller's deterministic fallback decides."""
    comp = Component(component)
    body = json.dumps(
        {"component": comp.value, **payload}, ensure_ascii=False, separators=(",", ":"), default=str
    )
    before = llm.usage()
    think = thinking_for(comp, payload)
    system = load_prompt(comp, prompts_dir)
    reply = llm.complete(
        body, system=system, max_tokens=THINKING_MAX_TOKENS(comp) if think else MAX_TOKENS[comp], thinking=think,
    )
    after = llm.usage()
    called = (after["calls"] + after["cache_hits"]) > (before["calls"] + before["cache_hits"])
    if think and called and (reply is None or extract_json(reply) is None):
        # A reasoning call that spent its whole cap thinking returns no
        # answer (9% of select calls on the first joint run). The classifier
        # mode answers the same question cheaply; a fallback rule should be
        # the last resort, not the price of an over-long thought.
        reply = llm.complete(body, system=system, max_tokens=MAX_TOKENS[comp], thinking=False)
    if reply is None:
        return None, called
    return extract_json(reply), True
