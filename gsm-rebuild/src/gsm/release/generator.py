"""Answer generation.

``DeterministicGenerator`` renders permitted projections verbatim. It is the
default, the test generator, and the safe fallback whenever a model draft is
blocked: its output can contain only text the disclosure plan approved.
"""

from __future__ import annotations

from collections.abc import Sequence

from gsm.domain.types import QueryAction, View
from gsm.providers.base import DraftResponse, GeneratorEvidence, GeneratorPlan, ProviderMetrics

CANONICAL_TEXT: dict[QueryAction, str] = {
    QueryAction.REFUSE: "I can't share that information.",
    QueryAction.NO_MEMORY: "I don't have any information about that.",
    QueryAction.ESCALATE: (
        "I can't answer that without additional authorization. "
        "Re-authenticate with a stronger method or declare the purpose of the request."
    ),
}

ANSWER_HEADER = "Based on shared memory:"
REDACTED_HEADER = "Based on shared memory (some details are withheld by policy):"


class DeterministicGenerator:
    name = "deterministic"

    def __init__(self) -> None:
        self.metrics = ProviderMetrics()

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        self.metrics.record(ok=True)
        if plan.action not in (QueryAction.ANSWER, QueryAction.ANSWER_REDACTED):
            return DraftResponse(action=plan.action, text=CANONICAL_TEXT[plan.action], generator=self.name)
        approved = {mid for mid, _view, _fields in plan.items}
        lines: list[str] = []
        cited: list[str] = []
        aggregates: dict[str, int] = {}
        for ev in evidence:
            if ev.memory_id not in approved:
                continue
            if ev.view is View.AGGREGATE:
                aggregates[ev.text] = aggregates.get(ev.text, 0) + 1
                continue
            lines.append(f"- {ev.text} [{ev.memory_id}]")
            cited.append(ev.memory_id)
        for label, count in sorted(aggregates.items()):
            lines.append(f"- {count} x {label}")
        header = ANSWER_HEADER if plan.action is QueryAction.ANSWER else REDACTED_HEADER
        return DraftResponse(
            action=plan.action,
            text="\n".join([header, *lines]),
            cited_memory_ids=tuple(cited),
            generator=self.name,
        )
