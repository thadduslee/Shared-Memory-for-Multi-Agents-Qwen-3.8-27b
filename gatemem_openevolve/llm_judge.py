"""LLM-as-a-judge, as GateMem defines it.

WHY THIS EXISTS.  The first version of `evaluator.py` scored with
`judge_gatemem.rule_score` alone.  That is not the benchmark's definition of
correct: GateMem's own scorer docstring, quoted at the top of
`Auto Research/nodes/judge_gatemem.py`, says "Primary evaluation should come
from LLM-as-a-judge.  This scorer provides ... auxiliary" scoring.  Running the
rule pass as the sole judge means every selection decision in an evolution run
-- which parent to breed, which child is better -- is made against a proxy the
benchmark calls auxiliary.

WHAT IS REUSED, VERBATIM, BY IMPORT.  The prompt (`SYSTEM_PROMPT`), the
per-checkpoint task (`build_task`) and the combination rule (`merge_verdicts`)
all come from `nodes/judge_gatemem.py`.  Nothing about the judgement is
re-specified here; this module is transport and concurrency only.  That matters
because `merge_verdicts` encodes an asymmetry that is easy to get wrong by
re-implementation:

  * LEAKS are monotone-worse -- either pass can condemn, neither can acquit.
    An LLM that could clear a leak the rules caught would hand the system under
    test a way to score by writing persuasive prose.
  * UTILITY inverts -- the MODEL is primary, because the rule pass cannot tell
    "the fact is absent" from "the fact is present in a different word order".

THE WALL.  This module reads the hidden annotations.  It runs on the scoring
side, in the evaluator's process, and nothing it sees ever reaches the
Developer: the Developer's `smoke` tool reports actions only, and the workspace
manifests are stripped before any model-authored code runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
ORCHESTRATOR = _HERE.parent / "Auto Research"
for _path in (str(_HERE), str(ORCHESTRATOR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nodes.judge_gatemem import SYSTEM_PROMPT, build_task, merge_verdicts  # noqa: E402

logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s JUDGE[%(process)d] %(message)s"))
    logger.addHandler(_h)
    logger.propagate = False
logger.setLevel(logging.INFO)

JUDGE_ENABLED = os.environ.get("GATEMEM_OE_LLM_JUDGE", "0") == "1"
JUDGE_BASE = os.environ.get("GATEMEM_JUDGE_API_BASE", "http://localhost:8002/v1")
JUDGE_KEY = os.environ.get("GATEMEM_JUDGE_API_KEY", "sk-local-dummy-key")
JUDGE_MODEL = os.environ.get("GATEMEM_JUDGE_MODEL", "Qwen/Qwen3.8-27B")
JUDGE_CONCURRENCY = int(os.environ.get("GATEMEM_JUDGE_CONCURRENCY", "8"))
JUDGE_MAX_TOKENS = int(os.environ.get("GATEMEM_JUDGE_MAX_TOKENS", "400"))

# THINKING IS OFF FOR THE JUDGE, AND IT HAS TO BE.
# The verdict is one line of JSON, but Qwen3 reasons about it until it runs out
# of budget: measured at 2500 tokens / 38s and STILL truncated mid-thought, so
# `content` came back None and the call fell back to the rule verdict. Half of
# a first smoke batch degraded that way -- silently, because a fallback looks
# like a judged checkpoint from the outside. With thinking off the same calls
# answer in 58-61 tokens and ~1.1s, and parse every time.
#
# This is the same thinking-disable the vLLM serve script cares about, applied
# per request rather than per model: `chat_template_kwargs` is vLLM's hook and
# it works regardless of the served slug.
JUDGE_NO_THINK = os.environ.get("GATEMEM_JUDGE_THINKING", "0") == "1"
JUDGE_TIMEOUT = int(os.environ.get("GATEMEM_JUDGE_TIMEOUT", "180"))

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _client():
    from openai import OpenAI

    return OpenAI(base_url=JUDGE_BASE, api_key=JUDGE_KEY, timeout=JUDGE_TIMEOUT)


def _parse(text: str) -> dict[str, Any] | None:
    """Pull the fenced verdict out of the reply.

    Returns None rather than a default verdict when nothing parses. An
    unparseable reply must fall back to the RULE verdict (`merge_verdicts`
    returns `rule` unchanged when handed an empty dict), because inventing a
    verdict here would silently move U for a reason that has nothing to do with
    the system under test.
    """
    if not text:
        return None
    match = _JSON_BLOCK.search(text)
    payload = match.group(1) if match else None
    if payload is None:
        # Some replies omit the fence. Take the outermost brace span.
        start, end = text.find("{"), text.rfind("}")
        payload = text[start : end + 1] if 0 <= start < end else None
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _judge_one(client, prediction: dict, annotation: dict, rule: dict) -> dict[str, Any]:
    """One checkpoint. Never raises: a judge failure falls back to the rule verdict."""
    try:
        response = client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_task(prediction, annotation, rule)},
            ],
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=0.0,
            **({} if JUDGE_NO_THINK else
               {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}),
        )
        verdict = _parse(response.choices[0].message.content or "")
    except Exception as exc:  # transport, timeout, rate limit
        logger.warning("judge call failed for %s: %r", prediction.get("checkpoint_id"), exc)
        verdict = None

    if verdict is None:
        # `merge_verdicts({}, ...)` returns the rule verdict untouched.
        return merge_verdicts(rule, {})
    return merge_verdicts(rule, verdict)


def judge_batch(
    items: list[tuple[dict, dict, dict]],
) -> tuple[list[dict[str, Any]], int]:
    """Judge many checkpoints concurrently.

    `items` is (prediction, annotation, rule_verdict) per checkpoint, and the
    return preserves that order so the caller can zip it back onto checkpoint
    ids. The second element is how many calls fell back to the rule verdict --
    a degraded-judge count, which the caller reports as an artifact: a run whose
    judge silently stopped answering would otherwise look like a run whose
    utility collapsed.
    """
    if not items:
        return [], 0

    client = _client()
    with ThreadPoolExecutor(max_workers=JUDGE_CONCURRENCY) as pool:
        verdicts = list(
            pool.map(lambda triple: _judge_one(client, *triple), items)
        )
    degraded = sum(1 for v in verdicts if not v.get("llm_judged"))
    if degraded:
        logger.warning("%d/%d checkpoints fell back to the rule verdict", degraded, len(items))
    return verdicts, degraded
