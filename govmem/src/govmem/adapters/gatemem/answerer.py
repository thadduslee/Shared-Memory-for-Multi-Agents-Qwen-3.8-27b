"""The benchmark's answerer: `bench/prompts/query_prompt.txt` rendered exactly as GateMem renders it,
answered by Qwen3.8-27B with thinking ON (all baselines ran that way)."""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from govmem.llm import ChatProtocol, ReplyCache, extract_json


def bench_domains(repo: Path) -> Any:
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from bench import domains

    return domains


@lru_cache(maxsize=8)
def _load(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def split_system_user(template: str) -> tuple[str, str]:
    if "[SYSTEM]" in template and "[REQUEST CONTEXT]" in template:
        before, after = template.split("[REQUEST CONTEXT]", 1)
        return before.replace("[SYSTEM]", "").strip(), ("[REQUEST CONTEXT]" + after).strip()
    return "", template.strip()


def format_memory_block(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "(none)"
    lines = []
    for i, item in enumerate(evidence, 1):
        speaker = str(item.get("principal_id") or item.get("role") or "unknown")
        text = str(item.get("text") or "").strip().replace("\n", " ")
        lines.append(f"Memory {i} (speaker={speaker}): {text}")
    return "\n".join(lines)


def render(repo: Path, domain: str, asker: dict[str, Any], query_text: str, evidence: list[dict[str, Any]],
           relationships: list[dict[str, Any]]) -> tuple[str, str]:
    d = bench_domains(repo)
    key = d.normalize_domain_name(domain)
    rel_block = "\n".join(d.format_relationship_fact(r or {}) for r in relationships) if relationships else "(none)"
    variables = {
        "asker_principal_id": asker.get("principal_id", "?"), "asker_role": asker.get("role", "?"),
        "relationship_facts_block": rel_block, "retrieved_memory_block": format_memory_block(evidence),
        "query_text": query_text, "domain_label": d.get_domain_label(key),
        "global_access_policy_block": d.get_query_policy_block(key),
    }
    system_t, user_t = split_system_user(_load(str(repo / "bench" / "prompts" / "query_prompt.txt")))
    return system_t.format(**variables), user_t.format(**variables)


def answer(chat: ChatProtocol, cache: ReplyCache, repo: Path, domain: str, asker: dict[str, Any], query_text: str,
           evidence: list[dict[str, Any]], relationships: list[dict[str, Any]], agent_action: str,
           attempts: int = 3, salt: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    system, user = render(repo, domain, asker, query_text, evidence, relationships)
    key = f"answer{salt}:" + json.dumps([system, user], ensure_ascii=False)
    import hashlib

    key = hashlib.sha256(key.encode()).hexdigest()
    meta: dict[str, Any] = {"prompt_chars": len(system) + len(user), "degraded": False, "cached": False}
    text = cache.get(key)
    if text is not None and extract_json(text) is not None:
        meta["cached"] = True
    else:
        text = ""
        for i in range(attempts):
            res = chat.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                            temperature=0.0, tag="answer")
            meta["usage"] = res.usage
            meta["attempts"] = i + 1
            if res.ok and extract_json(res.text) is not None:
                text = res.text
                cache.put(key, text)
                break
            meta["error"] = (res.error or "no JSON in reply")[:300]
    joined = " ".join(str(e.get("text") or "") for e in evidence).strip()
    if not text:
        meta["degraded"] = True
        return {"action": agent_action, "answer": joined if agent_action in ("answer", "answer_redacted") else "",
                "answer_structured": {}, "used_record_ids": [e["record_id"] for e in evidence]}, meta
    parsed = extract_json(text) or {}
    action = str(parsed.get("action") or agent_action)
    ans = parsed.get("answer")
    ans = ans if isinstance(ans, str) else json.dumps(ans) if ans is not None else ""
    structured = parsed.get("answer_structured")
    ids = parsed.get("used_record_ids")
    return {"action": action, "answer": ans,
            "answer_structured": structured if isinstance(structured, dict) else {},
            "used_record_ids": [str(x) for x in ids] if isinstance(ids, list) else []}, meta
