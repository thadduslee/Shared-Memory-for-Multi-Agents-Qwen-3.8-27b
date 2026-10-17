"""A deterministic stand-in for every outer-loop model call.

WHY RULE-BASED AND NOT CANNED. The fake has to drive the REAL code paths --
prompt rendering, JSON extraction, verdict merging, delta application, the gate
-- so its replies are produced the way a model's would be (text with a JSON
block in it) from the same inputs, by a small rule per role:

  evaluator (answerer)  the agent's own action, and an answer that concatenates
                        the evidence texts. With it, a utility checkpoint passes
                        exactly when the memory released the right records.
  judge                 every field null. `merge_verdicts` then returns the rule
                        verdict unchanged: the judge is the rule scorer, but the
                        LLM-judge path (render, parse, normalise, merge) runs.
  reflector             the component with the most attributed failures, skipping
                        a (component, mechanism) pair already rejected, as one
                        Markdown diagnosis with the JSON block.
  curator               appends one bullet derived from the diagnosis's
                        `mechanism`.

The facts a prompt does not carry in parseable form (the agent's action, the
attribution counts, the rejections) arrive through `chat(context=...)`, which
the real client ignores.

The memory side (inside each episode process) is not this module's: evaluate.py
calls `memory_system.fake_llm.install()` in the child when SHM_FAKE_LLM is set
and that module exists. `install()` here covers the parent.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from contracts import Component
from loop import config as config_mod
from loop.llm_client import ChatResult, set_llm_client


def _usage(messages: list[dict[str, Any]], reply: str) -> dict[str, int]:
    n_in = sum(len(str(m.get("content") or "")) for m in messages) // 4
    n_out = len(reply) // 4
    return {"input_tokens": n_in, "output_tokens": n_out, "total_tokens": n_in + n_out}


def fake_mechanism(component: str, op: str, outcome: str, n: int) -> str:
    """Domain-free by construction: only op names, outcomes and a count."""
    return (f"{n} failures trace to {component} decisions of kind {op} ({outcome}); "
            f"state explicitly when a {op} decision must widen or narrow who can read the result")


class FakeLLMClient:
    is_fake = True

    def __init__(self) -> None:
        self._usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.calls: list[dict[str, Any]] = []

    @property
    def usage_total(self) -> dict[str, int]:
        return dict(self._usage_total)

    async def aclose(self) -> None:
        return None

    async def chat(
        self, *, route: str, model: str, messages: list[dict[str, Any]],
        temperature: float = 0.2, max_tokens: int | None = None,
        timeout_s: float | None = None, role: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ChatResult:
        ctx = context or {}
        handler = {
            "evaluator": self._answerer, "judge": self._judge,
            "reflector": self._reflector, "curator": self._curator,
        }.get(role or "")
        if handler is None:
            reply = "{}"
        else:
            reply = handler(messages, ctx)
        usage = _usage(messages, reply)
        for k, v in usage.items():
            self._usage_total[k] += v
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:12]
        self.calls.append({"role": role, "route": route, "prompt_hash": digest})
        return ChatResult(text=reply, model=f"fake:{model}", route=route, usage=usage, finish_reason="stop")

    # ------------------------------------------------------------ roles

    @staticmethod
    def _answerer(messages: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
        action = str(ctx.get("agent_action") or "no_memory")
        evidence = [e for e in ctx.get("evidence") or [] if isinstance(e, dict)]
        answer = " ".join(str(e.get("text") or "").strip() for e in evidence).strip()
        if action not in ("answer", "answer_redacted"):
            answer = "I can't share that." if action == "refuse" else "There is no memory of that."
        body = {"action": action, "answer": answer, "answer_structured": {},
                "used_record_ids": [str(e.get("record_id") or "") for e in evidence]}
        # Wrapped like a reasoning reply, so the think-stripping path runs too.
        return "<think>fake reasoning {\"action\": \"refuse\"}</think>\n```json\n" + json.dumps(body) + "\n```"

    @staticmethod
    def _judge(messages: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
        return json.dumps({"action_ok": None, "utility_ok": None, "privacy_leak": None,
                           "deletion_leak": None, "notes": "fake judge: rule verdict stands"})

    @staticmethod
    def _reflector(messages: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
        attribution = ctx.get("attribution") or {}
        rejected = {(r.get("component"), r.get("mechanism")) for r in ctx.get("rejections") or []}
        by_cop: dict[str, int] = attribution.get("by_component_op_outcome") or {}
        ranked: list[tuple[int, str, str, str]] = []
        for key, n in by_cop.items():
            comp, op, outcome = (key.split("|") + ["", "", ""])[:3]
            if comp in {c.value for c in Component}:
                ranked.append((int(n), comp, op, outcome))
        ranked.sort(key=lambda r: (-r[0], r[1], r[2], r[3]))
        # Components with no attributed failures come last, in a fixed order,
        # so a rejected mechanism always has somewhere else to go.
        for comp in Component:
            ranked.append((0, comp.value, "noop", "applied"))
        choice = ranked[0]
        for row in ranked:
            mech = fake_mechanism(row[1], row[2], row[3], row[0])
            if (row[1], mech) not in rejected:
                choice = row
                break
        n, comp, op, outcome = choice
        mechanism = fake_mechanism(comp, op, outcome, n)
        evidence = [f["checkpoint_id"] for f in ctx.get("failures") or []
                    if f.get("attributed_component") == comp][:5]
        block = {"component": comp, "mechanism": mechanism, "evidence_ids": evidence,
                 "expected_effect": {"U": "+"}}
        return (f"## Diagnosis\n\nThe largest attributed group is `{comp}` / `{op}` ({outcome}), "
                f"{n} failure(s).\n\n```json\n{json.dumps(block)}\n```\n")

    @staticmethod
    def _curator(messages: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
        mechanism = str(ctx.get("mechanism") or "be explicit about the decision that failed")
        # One sentence, no braces (prompts may be format templates).
        text = mechanism.split(";")[-1].strip().replace("{", "(").replace("}", ")")
        text = text[:1].upper() + text[1:]
        if not text.endswith("."):
            text += "."
        return "```json\n" + json.dumps([{"op": "add", "anchor": None, "text": f"- {text}"}]) + "\n```"


def install() -> FakeLLMClient:
    """Make every outer-loop call in THIS process fake, and mark the
    environment so episode processes install the memory-side fake."""
    os.environ["SHM_FAKE_LLM"] = "1"
    config_mod.reload()
    client = FakeLLMClient()
    set_llm_client(client)
    return client
