"""Disclosure planning: choose the response action and, per memory, the view.

The planner only ever selects among views the policy engine already
permitted; it cannot raise a view. The plan lists hidden *field names* and
semantic *categories*, never hidden values.

Action semantics:

* ANSWER           - relevant evidence exists and every item is a full view.
* ANSWER_REDACTED  - relevant evidence exists and at least one item is limited.
* ESCALATE         - nothing answerable, but a relevant record would become
                     available after step-up authentication / declaring purpose.
* REFUSE           - nothing answerable, and a relevant record exists whose
                     policy allows acknowledging it to this requester.
* NO_MEMORY        - otherwise. Deliberately indistinguishable from "a protected
                     record exists that you may not learn about".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from gsm import POLICY_ENGINE_VERSION
from gsm.disclosure.schemas import get_schema
from gsm.domain.context import PrincipalContext
from gsm.domain.reasons import Reason
from gsm.domain.types import QueryAction, View
from gsm.ids import new_id
from gsm.providers.base import GeneratorPlan
from gsm.retrieval.evidence import EvidenceBundle


class DisclosureItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: str
    version_id: str
    view: View
    allowed_fields: tuple[str, ...]
    prohibited_fields: tuple[str, ...]
    prohibited_categories: tuple[str, ...]
    reason: str
    policy_version: str


class DisclosurePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    tenant_id: str
    workspace_id: str
    principal_id: str
    action: QueryAction
    items: tuple[DisclosureItem, ...]
    reasons: tuple[Reason, ...]
    policy_version: str

    @property
    def approved_ids(self) -> frozenset[str]:
        return frozenset(i.memory_id for i in self.items)

    @property
    def prohibited_categories(self) -> tuple[str, ...]:
        cats: set[str] = set()
        for i in self.items:
            cats |= set(i.prohibited_categories)
        return tuple(sorted(cats))

    def for_generator(self) -> GeneratorPlan:
        return GeneratorPlan(
            action=self.action,
            items=tuple((i.memory_id, i.view, i.allowed_fields) for i in self.items),
            prohibited_categories=self.prohibited_categories,
        )

    def restricted_to(self, memory_ids: frozenset[str]) -> DisclosurePlan:
        items = tuple(i for i in self.items if i.memory_id in memory_ids)
        if not items:
            action = QueryAction.NO_MEMORY
        elif all(i.view is View.FULL for i in items):
            action = QueryAction.ANSWER
        else:
            action = QueryAction.ANSWER_REDACTED
        return self.model_copy(update={"items": items, "action": action})


def plan_disclosure(
    ctx: PrincipalContext,
    bundle: EvidenceBundle,
    *,
    refusal_relevant: bool,
    escalation_relevant: bool,
) -> DisclosurePlan:
    items: list[DisclosureItem] = []
    for ev in bundle.items:
        schema = get_schema(ev.content_schema)
        spec = schema.views[ev.view]
        hidden = [n for n in schema.field_names() if n not in spec.fields]
        cats = sorted({c for n in hidden if (c := schema.spec(n).category) is not None})
        if schema.text_allowed and not spec.include_text:
            hidden.append("text")
            if schema.text_category:
                cats = sorted({*cats, schema.text_category})
        items.append(
            DisclosureItem(
                memory_id=ev.memory_id,
                version_id=ev.version_id,
                view=ev.view,
                allowed_fields=tuple(spec.fields) + (("text",) if spec.include_text else ()),
                prohibited_fields=tuple(hidden),
                prohibited_categories=tuple(cats),
                reason=(Reason.EVIDENCE_AVAILABLE if ev.view is View.FULL else Reason.PARTIAL_VIEW_ONLY).value,
                policy_version=ev.policy_version,
            )
        )
    if items:
        full = all(i.view is View.FULL for i in items)
        action = QueryAction.ANSWER if full else QueryAction.ANSWER_REDACTED
        reasons = (Reason.EVIDENCE_AVAILABLE,) if full else (Reason.EVIDENCE_AVAILABLE, Reason.PARTIAL_VIEW_ONLY)
    elif escalation_relevant:
        action, reasons = QueryAction.ESCALATE, (Reason.STEP_UP_REQUIRED,)
    elif refusal_relevant:
        action, reasons = QueryAction.REFUSE, (Reason.EXISTENCE_ACKNOWLEDGED,)
    else:
        action, reasons = QueryAction.NO_MEMORY, (Reason.NO_USABLE_MEMORY,)
    return DisclosurePlan(
        plan_id=new_id("plan"),
        tenant_id=ctx.tenant_id,
        workspace_id=ctx.workspace_id,
        principal_id=ctx.principal_id,
        action=action,
        items=tuple(items),
        reasons=reasons,
        policy_version=POLICY_ENGINE_VERSION,
    )
