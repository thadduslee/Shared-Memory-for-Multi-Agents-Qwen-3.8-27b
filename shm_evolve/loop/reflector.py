"""The Reflector: from attributed failures to ONE component and ONE mechanism.

DESIGN.md §3.3. It reads what the judge and the attribution produced -- never
the hidden annotations' leak targets or include lists -- and names a single
component, because a delta that touches two prompts cannot be credited or
blamed by a paired gate on one run: the gate measures the joint change, and the
next round would not know which half to keep.

WHAT IT SEES, and why each part:
  * the attribution report (counts, the requester-group saturation table, the
    per-component prompt-rejection rates) -- the aggregate the choice must be
    grounded in;
  * a STRATIFIED sample of failure rows (round-robin across attributed
    components, deterministic), each with its decision chain, the question,
    the action taken and an excerpt of what was released -- enough to see the
    mechanism, capped so the prompt stays bounded on a 579-checkpoint run;
  * the score history and the last few REJECTED proposals -- the old loop's
    Architect retried the same idea for nine rounds because nothing told it the
    gate had already said no;
  * all four current prompts, since the mechanism often lives at a hand-off
    (the front door's audience is what the processor's grant starts from).

Its own prompt (`loop/prompts/reflector.md`) is frozen and carries no dataset
vocabulary; the rows it is shown do carry dataset words, and the curator's
lint is what stops them from reaching an evolvable prompt.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from contracts import AttributedFailure, Component
from loop import config as config_mod
from loop.config import Config
from loop.jsonx import extract_json, write_json
from loop.llm_client import LLMClientProtocol

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "reflector.md"
VALID_EFFECT_KEYS = ("U", "A", "F")


@dataclass
class Diagnosis:
    component: str
    mechanism: str
    evidence_ids: list[str] = field(default_factory=list)
    expected_effect: dict[str, str] = field(default_factory=dict)
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_diagnosis(text: str) -> tuple[Diagnosis | None, str]:
    """(diagnosis, problem). The LAST JSON object wins: prose before it may
    quote an example block."""
    block = extract_json(text, want=dict, last=True)
    if not block:
        return None, "no JSON block"
    comp = str(block.get("component") or "").strip()
    if comp not in {c.value for c in Component}:
        return None, f"component {comp!r} is not one of {[c.value for c in Component]}"
    mechanism = str(block.get("mechanism") or "").strip()
    if not mechanism:
        return None, "empty mechanism"
    ev = block.get("evidence_ids")
    eff = block.get("expected_effect")
    markdown = text.rsplit("```", 2)[0] if "```" in text else text
    return Diagnosis(
        component=comp, mechanism=mechanism[:600],
        evidence_ids=[str(x) for x in ev][:20] if isinstance(ev, list) else [],
        expected_effect={k: str(v) for k, v in eff.items() if k in VALID_EFFECT_KEYS and v in ("+", "-")}
        if isinstance(eff, dict) else {},
        markdown=markdown.strip()[:4000],
    ), ""


def sample_failures(failures: list[AttributedFailure], n: int) -> list[AttributedFailure]:
    """Round-robin across attributed components, largest group first, ids
    sorted within: every component with failures is represented, and the
    sample is the same for the same inputs."""
    groups: dict[str, list[AttributedFailure]] = {}
    for f in sorted(failures, key=lambda f: f.checkpoint_id):
        groups.setdefault(f.attributed_component, []).append(f)
    order = sorted(groups, key=lambda c: (-len(groups[c]), c))
    out: list[AttributedFailure] = []
    i = 0
    while len(out) < n and any(i < len(groups[c]) for c in order):
        for c in order:
            if i < len(groups[c]) and len(out) < n:
                out.append(groups[c][i])
        i += 1
    return out


def _compact_event(ev: dict[str, Any]) -> str:
    args = json.dumps(ev.get("op_args") or {}, sort_keys=True, default=str)
    if len(args) > 160:
        args = args[:157] + "..."
    bits = [f"#{ev.get('seq')}", f"{ev.get('component')}.{ev.get('op')}", str(ev.get("outcome")),
            "llm" if ev.get("llm_called") else "rule"]
    if ev.get("signal_kind"):
        bits.append(f"signal={ev['signal_kind']}")
    if ev.get("reason"):
        bits.append(f"reason={str(ev['reason'])[:80]!r}")
    bits.append(f"records={len(ev.get('record_ids') or [])}")
    if ev.get("denied_ids"):
        bits.append(f"denied={len(ev['denied_ids'])}")
    return " ".join(bits) + f" args={args}"


def _failure_block(f: AttributedFailure, pred: dict[str, Any], visible: dict[str, Any]) -> str:
    out = pred.get("output") or {}
    debug = out.get("debug") or {}
    evidence = debug.get("evidence") or []
    v = f.verdict
    lines = [
        f"### {f.checkpoint_id}",
        f"- type: {f.query_type}; asker role: {f.asker_role}; expected action: {v.get('expected_action')}; "
        f"memory action: {debug.get('agent_action', '?')}; final action: {out.get('action')}",
        f"- verdict: {v.get('reason') or 'failed'}",
        f"- question: {str(visible.get('query_text') or '')[:300]}",
        f"- released {len(f.released)}, denied {len(f.denied)}",
        f"- attributed: {f.attributed_component}.{f.attributed_op} -- {f.note}",
    ]
    for item in evidence[:3]:
        lines.append(f"  - released ({item.get('principal_id') or item.get('role') or '?'}): "
                     f"{str(item.get('text') or '')[:200]}")
    lines.append("- chain:")
    lines += [f"  - {_compact_event(ev)}" for ev in f.chain[-12:]]
    return "\n".join(lines)


def build_user_prompt(
    *, attribution: dict[str, Any], failures: list[AttributedFailure], predictions: dict[str, dict[str, Any]],
    visible: dict[str, dict[str, Any]], prompts: dict[str, str], history: list[dict[str, Any]],
    rejections: list[dict[str, Any]], cfg: Config,
) -> str:
    sample = sample_failures(failures, cfg.reflector_failure_sample)
    hist = history[-cfg.reflector_history_rows:]
    parts = ["## Score history (oldest first)"]
    if hist:
        for row in hist:
            s = row.get("champion") or {}
            parts.append(
                f"- iter {row.get('iteration')}: champion r{row.get('head_before')} "
                f"U={s.get('utility', 0):.3f} A={s.get('access_violation', 0):.3f} "
                f"F={s.get('forgetting_failure', 0):.3f} MGS={s.get('mgs', 0):.3f}; "
                f"proposal {row.get('component') or '-'} -> {'ACCEPTED' if row.get('accepted') else 'rejected'}"
                f" ({row.get('reason', '')[:120]})")
    else:
        parts.append("- (first iteration)")
    trimmed = {k: v for k, v in attribution.items() if k not in ("trace_totals",)}
    parts += ["", "## Attribution report", "```json", json.dumps(trimmed, indent=1, sort_keys=True)[:12000], "```"]
    parts += ["", "## Rejected proposals (most recent last)"]
    shown = rejections[-cfg.reflector_rejections_shown:]
    parts += [f"- iter {r.get('iteration')}: {r.get('component')} -- {r.get('mechanism', '')[:300]} "
              f"[{r.get('reason', '')[:160]}]" for r in shown] or ["- (none)"]
    parts += ["", f"## Failure sample ({len(sample)} of {len(failures)})"]
    parts += [_failure_block(f, predictions.get(f.checkpoint_id, {}), visible.get(f.checkpoint_id, {}))
              for f in sample] or ["(no failures)"]
    parts += ["", "## Current prompts"]
    for comp in Component:
        text = prompts.get(comp.value, "")
        parts += [f"### {comp.value}", "```markdown", text[: cfg.max_prompt_chars] or "(empty)", "```"]
    return "\n".join(parts)


async def reflect(
    *, client: LLMClientProtocol, attribution: dict[str, Any], failures: list[AttributedFailure],
    predictions: list[dict[str, Any]], visible: dict[str, dict[str, Any]], prompts: dict[str, str],
    history: list[dict[str, Any]], rejections: list[dict[str, Any]], out_dir: Path, cfg: Config | None = None,
) -> tuple[Diagnosis | None, str]:
    """Returns (diagnosis, problem). Retries once with the parse problem
    appended, because the commonest failure is a missing or malformed block."""
    cfg = cfg or config_mod.get()
    system = PROMPT_PATH.read_text(encoding="utf-8")
    user = build_user_prompt(attribution=attribution, failures=failures,
                             predictions={p["checkpoint_id"]: p for p in predictions}, visible=visible,
                             prompts=prompts, history=history, rejections=rejections, cfg=cfg)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompt.md").write_text(f"# system\n\n{system}\n\n# user\n\n{user}\n", encoding="utf-8")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    context = {"attribution": attribution, "rejections": rejections,
               "failures": [asdict(f) for f in sample_failures(failures, cfg.reflector_failure_sample)]}
    problem = ""
    replies = []
    for _attempt in range(2):
        result = await client.chat(route=cfg.reflector_route, model=cfg.reflector_model, messages=messages,
                                   temperature=0.3, max_tokens=cfg.reflector_max_tokens, role="reflector",
                                   timeout_s=cfg.optimizer_timeout_s, context=context)
        replies.append(result.text or f"(error: {result.error})")
        if not result.ok:
            problem = f"reflector call failed: {result.error}"
            continue
        diag, problem = parse_diagnosis(result.text)
        if diag is not None:
            (out_dir / "reply.md").write_text("\n\n---\n\n".join(replies), encoding="utf-8")
            write_json(out_dir / "diagnosis.json", diag.to_dict())
            return diag, ""
        messages = messages + [{"role": "assistant", "content": result.text},
                               {"role": "user", "content": f"Your reply could not be used: {problem}. "
                                "End with exactly one fenced JSON block in the required shape."}]
    (out_dir / "reply.md").write_text("\n\n---\n\n".join(replies), encoding="utf-8")
    write_json(out_dir / "diagnosis.json", {"error": problem})
    return None, problem
