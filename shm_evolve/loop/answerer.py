"""The benchmark's answerer: GateMem's own query_prompt.txt over our evidence.

Ported from `Auto Research/prompts_gatemem.py` and the render half of
`nodes/medical_evaluator.py`. FROZEN by design (DESIGN.md §4).

WHY THE FILE AND NOT OUR WORDING. The answerer IS the system under test as the
benchmark defines it. A prompt we wrote -- however much better it scores --
silently redefines the measurement, and the number stops being comparable with
the paper or the baselines. So the template is loaded from
`bench/prompts/query_prompt.txt` and the only transformations are the ones
GateMem's runner makes: split on `[REQUEST CONTEXT]` into system/user halves,
substitute the documented placeholders. Domain label, policy block and the
relationship-fact format come from `bench.domains` (imported via GATEMEM_REPO),
not restated here.

WHY THINKING IS ON. Every baseline's answerer reasoned (vLLM's Qwen3 template
default). Forcing it off scored our memory against a different answerer: on 27
office checkpoints the champion hard-refused with the right evidence in hand,
thinking off answered 3, thinking on 22. `config.vllm_chat_template_kwargs`
leaves the template default for role "evaluator" when EVALUATOR_THINKING=true.

THE SPEAKER TRAVELS WITH THE TEXT. The prompt renders each record as
"Memory N (speaker=...)" and asks the model to decide authorisation by who owns
what. Evidence without a speaker rendered as `speaker=unknown` and the answerer
refused what it could not attribute (18% answered vs 35-39% for every baseline
on the same prompt). `format_memory_block` falls back through principal_id,
speaker, author_id, role.

FALLBACK. A render that fails after EVAL_RENDER_ATTEMPTS whole-call retries
serves the agent's own action with the joined evidence texts and is marked
`degraded` in `render`, so the report can say how much of a score was not
model-written (a run once reported MGS 0.84 with every render degraded).
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from loop import config as config_mod
from loop.config import Config
from loop.jsonx import extract_json
from loop.llm_client import LLMClientProtocol

QUERY_PROMPT = "query_prompt.txt"
# NOT judge_prompt_gatemem.txt -- see loop/judge.py.
JUDGE_PROMPT = "judge_prompt.txt"


def ensure_bench_importable(cfg: Config | None = None) -> None:
    root = str((cfg or config_mod.get()).gatemem_repo)
    if root not in sys.path:
        sys.path.insert(0, root)


def bench_domains(cfg: Config | None = None) -> Any:
    """`bench.domains`, imported from the GateMem checkout. It is stdlib-only;
    the rest of `bench` is not imported (bench.eval pulls sklearn)."""
    ensure_bench_importable(cfg)
    from bench import domains

    return domains


@lru_cache(maxsize=8)
def _load(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"GateMem prompt {path} is unreadable ({exc}). Set GATEMEM_REPO to a checkout with bench/prompts/."
        ) from exc


def prompt_path(filename: str, override: str, cfg: Config | None = None) -> str:
    cfg = cfg or config_mod.get()
    return override or str(cfg.gatemem_repo / "bench" / "prompts" / filename)


def split_system_user(template: str) -> tuple[str, str]:
    """GateMem's own split (bench/agents/base.py `_split_system_user`):
    before `[REQUEST CONTEXT]` is the system half with `[SYSTEM]` removed; the
    rest, marker included, is the user half. No markers -> whole as user."""
    if "[SYSTEM]" in template and "[REQUEST CONTEXT]" in template:
        before, after = template.split("[REQUEST CONTEXT]", 1)
        return before.replace("[SYSTEM]", "").strip(), ("[REQUEST CONTEXT]" + after).strip()
    return "", template.strip()


def format_relationship_facts(relationships: list[dict[str, Any]] | None, cfg: Config | None = None) -> str:
    """GateMem's format, including its `(none)` sentinel: an empty block reads
    as "this requester has no relationships", a stronger claim."""
    if not relationships:
        return "(none)"
    domains = bench_domains(cfg)
    return "\n".join(domains.format_relationship_fact(rel or {}) for rel in relationships)


def format_memory_block(evidence: list[dict[str, Any]] | None) -> str:
    """One numbered, speaker-attributed line per record, newlines flattened so
    one record cannot look like several (bench `_format_retrieved_memory_block`)."""
    if not evidence:
        return "(none)"
    lines: list[str] = []
    for index, item in enumerate(evidence, 1):
        speaker = str(item.get("principal_id") or item.get("speaker") or item.get("author_id")
                      or item.get("role") or "unknown")
        text = str(item.get("text") or "").strip().replace("\n", " ")
        lines.append(f"Memory {index} (speaker={speaker}): {text}")
    return "\n".join(lines)


def render_query_prompt(record: dict[str, Any], cfg: Config | None = None) -> tuple[str, str]:
    """`(system, user)` for one checkpoint. `record` holds only visible data:
    asker, query_text, evidence, the requester's relationships, the domain."""
    cfg = cfg or config_mod.get()
    domains = bench_domains(cfg)
    # The EPISODE's domain: defaulting to one domain once sent the medical
    # policy block with every office checkpoint -- a wrong measurement.
    domain_key = domains.normalize_domain_name(record.get("domain") or "")
    asker = record.get("asker") or {}
    variables = {
        "asker_principal_id": asker.get("principal_id", "?"),
        "asker_role": asker.get("role", "?"),
        "relationship_facts_block": format_relationship_facts(record.get("relationships"), cfg),
        "retrieved_memory_block": format_memory_block(record.get("evidence")),
        "query_text": record.get("query_text", ""),
        "domain_label": domains.get_domain_label(domain_key),
        "global_access_policy_block": domains.get_query_policy_block(domain_key),
    }
    system_t, user_t = split_system_user(_load(prompt_path(QUERY_PROMPT, cfg.evaluator_prompt_file, cfg)))
    try:
        return system_t.format(**variables), user_t.format(**variables)
    except KeyError as exc:
        raise ValueError(f"query_prompt.txt wants a variable this adapter lacks: {exc.args[0]!r}") from exc


def _is_context_overflow(error: str) -> bool:
    low = (error or "").lower()
    return any(s in low for s in ("maximum context length", "context length", "too many tokens",
                                  "prompt is too long", "input_tokens"))


def _error_kind(error: str) -> str:
    if not error:
        return "no_response"
    if _is_context_overflow(error):
        return "context_overflow"
    if "http 4" in error.lower():
        return "rejected"
    return "no_response"


async def render_answer(client: LLMClientProtocol, record: dict[str, Any],
                        cfg: Config | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn the agent's evidence into the benchmark's output dict.

    Returns (output, render_meta). `output` has action/answer/answer_structured/
    used_record_ids; `render_meta` has usage, attempts, prompt size and, on a
    fallback, `degraded` with the error kind.
    """
    cfg = cfg or config_mod.get()
    system_prompt, user_prompt = render_query_prompt(record, cfg)
    messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [
        {"role": "user", "content": user_prompt}]
    agent_action = str(record.get("action") or "refuse")
    evidence = [e for e in (record.get("evidence") or []) if isinstance(e, dict)]
    meta: dict[str, Any] = {"prompt_chars": len(system_prompt) + len(user_prompt), "attempts": 0,
                            "usage": {}, "degraded": False, "n_evidence": len(evidence)}
    text, last_error = "", ""
    attempts = max(1, cfg.eval_render_attempts)
    for attempt in range(attempts):
        meta["attempts"] = attempt + 1
        result = await client.chat(
            route="vllm", model=cfg.evaluator_model, messages=messages, temperature=0.0,
            max_tokens=cfg.evaluator_max_tokens, timeout_s=cfg.evaluator_timeout_s, role="evaluator",
            context={"agent_action": agent_action, "evidence": evidence},
        )
        for k, v in (result.usage or {}).items():
            meta["usage"][k] = meta["usage"].get(k, 0) + int(v or 0)
        if result.ok and extract_json(result.text, want=dict) is not None:
            text, last_error = result.text, ""
            break
        last_error = result.error or "reply carried no JSON object"
        if _is_context_overflow(last_error):
            break  # a property of the prompt, not the moment: retrying is identical
        if attempt + 1 < attempts:
            # Whole-call retries with a FLOOR (never under half the ceiling), so
            # each lands in a later rate-limit window, outside the semaphore.
            ceiling = cfg.eval_retry_base_s * (2 ** attempt)
            await asyncio.sleep(random.uniform(ceiling / 2, ceiling))

    joined = " ".join(str(e.get("text") or "").strip() for e in evidence).strip()
    used = [str(e.get("record_id") or "") for e in evidence]
    if not text:
        meta.update(degraded=True, error=last_error[:400], error_kind=_error_kind(last_error))
        return {"action": agent_action, "answer": joined if agent_action in ("answer", "answer_redacted") else "",
                "answer_structured": {}, "used_record_ids": used}, meta

    parsed = extract_json(text, want=dict) or {}
    action = str(parsed.get("action") or agent_action)
    answer = parsed.get("answer")
    answer = answer if isinstance(answer, str) else json.dumps(answer) if answer is not None else ""
    if action in ("answer", "answer_redacted") and not answer.strip():
        # An answering action with no text is a guaranteed utility miss on a
        # checkpoint the memory had already cleared; the gated texts are the
        # honest fallback.
        answer = joined
        meta["empty_answer_filled"] = True
    structured = parsed.get("answer_structured")
    ids = parsed.get("used_record_ids")
    meta["reply_excerpt"] = text[:700]
    return {
        "action": action,
        "answer": answer,
        "answer_structured": structured if isinstance(structured, dict) else {},
        "used_record_ids": [str(i) for i in ids] if isinstance(ids, list) and ids else used,
    }, meta
