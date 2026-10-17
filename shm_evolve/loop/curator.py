"""The Curator: a diagnosis becomes a small, checkable delta on one prompt.

DESIGN.md §3.4, ACE-style. The curator never rewrites a prompt; it emits at
most MAX_EDITS line edits against the prompt's BULLET structure, each anchored
on an existing line copied exactly. Why so constrained:

  * A full rewrite drifts. Every regenerated prompt loses phrasing that earlier
    accepted deltas put there for a reason, and the paired gate then credits or
    blames a change nobody can name. A delta is the unit the release chain
    stores and the next reflector reads.
  * EXACT anchors make an edit either apply or visibly fail. A fuzzy match
    would "succeed" on the wrong line; an anchor miss is recorded as a rejected
    edit with its reason, which the trace of the optimiser keeps.

Application semantics (sequential; each edit sees the result of the previous):
  add      anchor None -> after the last bullet line, at the shallowest bullet
           indentation (or at the end if there are no bullets);
           anchor given -> right after that line, at its indentation.
  replace  anchor must be a bullet line; replaced at its indentation.
  remove   anchor must be a bullet line; deleted.
An anchor matches a line equal to it after trailing whitespace is dropped; a
line number prefix ("12: ") copied from the shown prompt is tolerated. Zero or
several matches reject the edit. `text` must be one line, gets a "- " prefix
if missing, may not contain braces (prompts may be `str.format` templates),
and may not duplicate an existing line. Edits past MAX_EDITS are rejected.

The result is linted (loop/lint.py): size, introduced dataset vocabulary,
bullets present, fences balanced, placeholders unchanged.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from contracts import PromptDelta, PromptEdit
from loop import config as config_mod
from loop.config import Config
from loop.jsonx import extract_json, write_json
from loop.lint import LintResult, Vocabulary, lint_prompt
from loop.llm_client import LLMClientProtocol
from loop.reflector import Diagnosis

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "curator.md"
_LINE_NO = re.compile(r"^\s*\d+\s*[:|]\s?")


def _is_bullet(line: str) -> bool:
    return line.lstrip().startswith("- ")


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _find(lines: list[str], anchor: str) -> list[int]:
    want = anchor.rstrip()
    hits = [i for i, line in enumerate(lines) if line.rstrip() == want]
    if not hits and _LINE_NO.match(anchor):
        want = _LINE_NO.sub("", anchor, count=1).rstrip()
        hits = [i for i, line in enumerate(lines) if line.rstrip() == want]
    return hits


def _normalise_text(text: str) -> tuple[str | None, str]:
    t = (text or "").strip()
    if not t:
        return None, "empty text"
    if "\n" in t:
        return None, "text must be one line"
    if "{" in t or "}" in t:
        return None, "text may not contain braces"
    if not t.startswith("- "):
        t = "- " + t.lstrip("-* ").strip()
    if len(t) <= 2:
        return None, "empty bullet"
    return t, ""


def parse_edits(payload: Any) -> tuple[list[PromptEdit], list[dict[str, Any]]]:
    """JSON (list, or {"edits": [...]}) -> PromptEdits + malformed entries."""
    if isinstance(payload, dict):
        payload = payload.get("edits", [payload])
    if not isinstance(payload, list):
        return [], [{"edit": payload, "reason": "edits must be a JSON list"}]
    edits: list[PromptEdit] = []
    bad: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("op") not in ("add", "replace", "remove"):
            bad.append({"edit": item, "reason": "not an edit object with op add|replace|remove"})
            continue
        anchor = item.get("anchor")
        edits.append(PromptEdit(op=item["op"], anchor=anchor if isinstance(anchor, str) else None,
                                text=str(item.get("text") or "")))
    return edits, bad


def apply_edits(prompt: str, edits: list[PromptEdit], max_edits: int
                ) -> tuple[str, list[PromptEdit], list[dict[str, Any]]]:
    """(new prompt, applied edits, rejected [{edit, reason}])."""
    trailing_nl = prompt.endswith("\n")
    lines = prompt.split("\n") if prompt else []
    if trailing_nl:
        lines = lines[:-1]
    applied: list[PromptEdit] = []
    rejected: list[dict[str, Any]] = []
    for idx, edit in enumerate(edits):
        def reject(reason: str, edit: PromptEdit = edit) -> None:
            rejected.append({"edit": asdict(edit), "reason": reason})

        if idx >= max_edits:
            reject(f"over MAX_EDITS={max_edits}")
            continue
        if edit.op in ("replace", "remove") and not edit.anchor:
            reject(f"{edit.op} needs an anchor")
            continue
        pos: int | None = None
        if edit.anchor:
            hits = _find(lines, edit.anchor)
            if not hits:
                reject("anchor not found")
                continue
            if len(hits) > 1:
                reject(f"anchor is ambiguous ({len(hits)} matching lines)")
                continue
            pos = hits[0]
            if edit.op in ("replace", "remove") and not _is_bullet(lines[pos]):
                reject(f"{edit.op} anchor is not a bullet line")
                continue
        if edit.op == "remove":
            del lines[pos]  # type: ignore[arg-type]
            applied.append(PromptEdit(op="remove", anchor=edit.anchor, text=""))
            continue
        text, problem = _normalise_text(edit.text)
        if text is None:
            reject(problem)
            continue
        existing = {line.strip() for line in lines}
        if text.strip() in existing:
            reject("duplicate of an existing line")
            continue
        if edit.op == "replace":
            lines[pos] = _indent(lines[pos]) + text  # type: ignore[index]
        elif pos is not None:
            lines.insert(pos + 1, (_indent(lines[pos]) if _is_bullet(lines[pos]) else "") + text)
        else:
            bullets = [i for i, line in enumerate(lines) if _is_bullet(line)]
            if bullets:
                # After the last bullet, at the SHALLOWEST bullet indentation:
                # an unanchored rule is a top-level rule, even when the list
                # happens to end in a nested one.
                top = min((_indent(lines[i]) for i in bullets), key=len)
                lines.insert(bullets[-1] + 1, top + text)
            else:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(text)
        applied.append(PromptEdit(op=edit.op, anchor=edit.anchor, text=text))
    new = "\n".join(lines) + ("\n" if trailing_nl or not prompt else "")
    return new, applied, rejected


@dataclass
class CurationResult:
    component: str
    old_prompt: str
    new_prompt: str
    delta: PromptDelta
    rejected_edits: list[dict[str, Any]] = field(default_factory=list)
    lint: LintResult | None = None
    reply: str = ""
    problem: str = ""

    @property
    def ok(self) -> bool:
        return (not self.problem and bool(self.delta.edits) and self.lint is not None and self.lint.ok
                and self.new_prompt != self.old_prompt)

    @property
    def reason(self) -> str:
        if self.problem:
            return self.problem
        if not self.delta.edits:
            why = "; ".join(r["reason"] for r in self.rejected_edits[:3]) or "no edits proposed"
            return f"no edit applied ({why})"
        if self.lint is not None and not self.lint.ok:
            return "lint: " + "; ".join(self.lint.problems)
        if self.new_prompt == self.old_prompt:
            return "delta left the prompt unchanged"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {"component": self.component, "ok": self.ok, "reason": self.reason, "delta": self.delta.to_dict(),
                "rejected_edits": self.rejected_edits, "lint": self.lint.to_dict() if self.lint else None,
                "old_chars": len(self.old_prompt), "new_chars": len(self.new_prompt)}


def curate_text(diag: Diagnosis, prompt: str, reply: str, *, vocab: Vocabulary, cfg: Config) -> CurationResult:
    """The deterministic half: parse the reply, apply, lint. Separate from the
    model call so tests (and a replay of a stored reply) need no client."""
    payload = extract_json(reply, want=list, last=True)
    if payload is None:
        payload = extract_json(reply, want=dict, last=True)
    if payload is None:
        return CurationResult(component=diag.component, old_prompt=prompt, new_prompt=prompt,
                              delta=PromptDelta(diag.component, [], diag.mechanism), reply=reply,
                              problem="curator reply carried no JSON edit list")
    edits, bad = parse_edits(payload)
    new, applied, rejected = apply_edits(prompt, edits, cfg.max_edits)
    lint = lint_prompt(new, vocab=vocab, max_chars=cfg.max_prompt_chars, previous=prompt)
    return CurationResult(component=diag.component, old_prompt=prompt, new_prompt=new,
                          delta=PromptDelta(component=diag.component, edits=applied, rationale=diag.mechanism),
                          rejected_edits=bad + rejected, lint=lint, reply=reply)


def _numbered(prompt: str) -> str:
    return "\n".join(f"{i + 1:>3}: {line}" for i, line in enumerate(prompt.split("\n")))


async def curate(*, client: LLMClientProtocol, diag: Diagnosis, prompt: str, vocab: Vocabulary,
                 out_dir: Path, cfg: Config | None = None) -> CurationResult:
    cfg = cfg or config_mod.get()
    system = (PROMPT_PATH.read_text(encoding="utf-8")
              .replace("MAX_EDITS", str(cfg.max_edits)).replace("MAX_PROMPT_CHARS", str(cfg.max_prompt_chars)))
    user = (f"## Diagnosis\n\ncomponent: {diag.component}\nmechanism: {diag.mechanism}\n"
            f"expected effect: {diag.expected_effect}\n\n{diag.markdown}\n\n"
            f"## Current prompt of `{diag.component}` ({len(prompt)} chars; line numbers are NOT part of a line)\n\n"
            f"```markdown\n{_numbered(prompt) if prompt else '(empty)'}\n```\n")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompt.md").write_text(f"# system\n\n{system}\n\n# user\n\n{user}\n", encoding="utf-8")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    context = {"component": diag.component, "mechanism": diag.mechanism, "prompt": prompt}
    result = await client.chat(
        route=cfg.curator_route, model=cfg.curator_model, messages=messages,
        temperature=0.2, max_tokens=cfg.curator_max_tokens, role="curator",
        timeout_s=cfg.optimizer_timeout_s, context=context,
    )
    if not result.ok or not (result.text or "").strip():
        # A reasoning model can spend the whole completion cap thinking and
        # hand back nothing (joint_v3 iteration 1: 16384 tokens, 0 content).
        # One more try at twice the cap costs minutes; a lost iteration costs
        # a probe.
        cap = (cfg.curator_max_tokens or 16384) * 2
        result = await client.chat(
            route=cfg.curator_route, model=cfg.curator_model, messages=messages,
            temperature=0.2, max_tokens=cap, role="curator",
            timeout_s=cfg.optimizer_timeout_s, context=context,
        )
    (out_dir / "reply.md").write_text(result.text or f"(error: {result.error})", encoding="utf-8")
    if not result.ok:
        cur = CurationResult(component=diag.component, old_prompt=prompt, new_prompt=prompt,
                             delta=PromptDelta(diag.component, [], diag.mechanism),
                             problem=f"curator call failed: {result.error}")
    else:
        cur = curate_text(diag, prompt, result.text, vocab=vocab, cfg=cfg)
    write_json(out_dir / "curation.json", cur.to_dict())
    return cur
