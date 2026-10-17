"""Executable acceptance scenarios (used by ``gsm demo`` and the e2e tests).

Each step records a content-free observation (actions, ids, counts) and whether
the invariant held. Nothing printed here contains memory content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from gsm.auth import TrustedAuthenticator
from gsm.clock import FrozenClock
from gsm.domain.content import Content
from gsm.domain.operations import (
    AddFact,
    DeletionRequest,
    DeriveRequest,
    NewFact,
    PolicyTemplateRef,
    UpdateFact,
)
from gsm.domain.types import QueryAction, SemanticType, Sensitivity, View
from gsm.errors import NotFoundError
from gsm.service import GovernedMemoryService, QueryRequest

DECISION_V1 = "Apollo will launch on October 14 using a blue-green rollout"
DECISION_V2 = "Apollo will launch on October 21 using a canary rollout"
SUMMARY = "Weekly summary: Apollo launches October 21 via canary"
LEAVE_REASON = "knee surgery recovery"


@dataclass
class Step:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    name: str
    steps: list[Step]
    ids: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)


def _expect_missing(fn: Any) -> bool:
    try:
        fn()
    except NotFoundError:
        return True
    return False


def run_apollo_lifecycle(
    service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock, tenant: str = "demo-apollo"
) -> ScenarioResult:
    s = service
    steps: list[Step] = []
    res = ScenarioResult("apollo-lifecycle", steps)

    def ctx(who: str) -> Any:
        return auth.context(tenant, who)

    def tick() -> None:
        clock.advance(timedelta(minutes=5))

    # 1-4. principals and relationships
    s.create_tenant(tenant, "Apollo demo tenant")
    for pid in ("alice", "bob", "carol"):
        s.create_principal(tenant, pid, pid.title(), roles=["engineer"] if pid != "carol" else ["manager"])
    s.add_relationship_admin(tenant, "alice", "owner_of", "project", "apollo")
    s.add_relationship_admin(tenant, "alice", "member_of", "project", "apollo")
    s.add_relationship_admin(tenant, "bob", "member_of", "project", "apollo")
    steps.append(Step("principals: alice owns apollo, bob member, carol outside", True))

    # 5. alice stores a decision
    r = s.commit_operations(
        ctx("alice"),
        [
            AddFact(
                fact=NewFact(
                    semantic_type=SemanticType.DECISION,
                    content=Content(text=DECISION_V1),
                    topic="Apollo launch decision",
                    attribute="launch_plan",
                    subject_ids=("project:apollo",),
                    sensitivity=Sensitivity.CONFIDENTIAL,
                    policy_template=PolicyTemplateRef(name="project", project_id="apollo"),
                )
            )
        ],
    )
    mid = r.results[0].memory_id or ""
    res.ids["decision"] = mid
    steps.append(Step("alice stores decision", r.status == "committed", {"receipt": r.receipt_id}))
    tick()

    # 6. bob retrieves it
    q = s.query(ctx("bob"), QueryRequest(query="What is the Apollo launch decision?"))
    steps.append(
        Step(
            "bob retrieves decision",
            q.action is QueryAction.ANSWER and "October 14" in q.answer,
            {"action": q.action.value},
        )
    )

    # 7. carol cannot retrieve it or infer it exists
    qc = s.query(ctx("carol"), QueryRequest(query="What is the Apollo launch decision?"))
    qn = s.query(ctx("carol"), QueryRequest(query="What is the Hermes launch decision?"))
    same = qc.model_dump(exclude={"correlation_id", "release_id"}) == qn.model_dump(
        exclude={"correlation_id", "release_id"}
    )
    direct = _expect_missing(lambda: s.get_memory(ctx("carol"), mid)) and _expect_missing(
        lambda: s.get_memory(ctx("carol"), "mem_" + "0" * 32)
    )
    steps.append(
        Step(
            "carol gets indistinguishable NO_MEMORY",
            qc.action is QueryAction.NO_MEMORY and same and direct,
            {"action": qc.action.value, "indistinguishable": same},
        )
    )
    tick()

    # 8-9. alice updates; bob sees only the current value
    u = s.commit_operations(ctx("alice"), [UpdateFact(memory_id=mid, content=Content(text=DECISION_V2))])
    steps.append(
        Step(
            "alice updates decision (new immutable version)",
            u.status == "committed",
            {"version": u.results[0].version_id},
        )
    )
    tick()
    q2 = s.query(ctx("bob"), QueryRequest(query="What is the Apollo launch decision?"))
    steps.append(
        Step(
            "bob sees only current value",
            "October 21" in q2.answer and "October 14" not in q2.answer,
            {"action": q2.action.value},
        )
    )

    # 10. authorized derived summary
    d = s.derive(
        ctx("bob"), DeriveRequest(parent_ids=(mid,), content=Content(text=SUMMARY), topic="Apollo weekly summary")
    )
    res.ids["summary"] = d.memory_id
    qs = s.query(ctx("bob"), QueryRequest(query="Apollo weekly summary canary"))
    steps.append(
        Step(
            "bob creates derived summary",
            d.inherits_parent_policy and any(e.memory_id == d.memory_id for e in qs.evidence),
            {"summary": d.memory_id},
        )
    )
    carol_summary = _expect_missing(lambda: s.get_memory(ctx("carol"), d.memory_id))
    steps.append(Step("summary inherits restrictions (carol cannot read)", carol_summary))
    tick()

    # 11-12. alice deletes the decision
    f = s.forget(ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    res.ids["deletion_receipt"] = f.receipt_id
    steps.append(
        Step(
            "alice deletes decision",
            f.versions_tombstoned == 2 and f.cascade_status == "completed",
            {"versions_tombstoned": f.versions_tombstoned, "descendants": f.descendants_quarantined},
        )
    )
    gone = all(
        _expect_missing(lambda who=who, m=m: s.get_memory(ctx(who), m))
        for who in ("alice", "bob")
        for m in (mid, d.memory_id)
    )
    q3 = s.query(ctx("bob"), QueryRequest(query="Apollo launch decision canary blue-green summary"))
    hist = _expect_missing(lambda: s.get_history(ctx("alice"), mid))
    with s.db.tenant_tx(tenant) as tx:
        emb = tx.scalar(
            "SELECT count(*) FROM embeddings WHERE tenant_id = :tenant_id AND memory_id = ANY(CAST(:ids AS text[]))",
            ids=[mid, d.memory_id],
        )
        live = tx.scalar(
            "SELECT count(*) FROM memory_versions WHERE tenant_id = :tenant_id "
            "AND memory_id = ANY(CAST(:ids AS text[])) AND (state <> 'tombstoned' OR content_text IS NOT NULL)",
            ids=[mid, d.memory_id],
        )
    steps.append(
        Step(
            "original, old versions, embeddings and summary unavailable",
            gone and hist and q3.action is QueryAction.NO_MEMORY and emb == 0 and live == 0,
            {"embeddings_left": emb, "unscrubbed_versions": live, "query_action": q3.action.value},
        )
    )

    # 13. repeat deletion and cascade
    again = s.forget(ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    rerun = s.run_cascades(tenant, include_completed=True)
    steps.append(
        Step(
            "repeated deletion and cascade are safe",
            again.versions_tombstoned == 0 and all(c.status == "completed" for c in rerun),
            {"cascade_runs": [c.attempts for c in rerun]},
        )
    )

    # 14. audit receipts without content
    audit = s.audit_export(tenant, limit=1000)
    blob = repr(audit)
    receipt = s.get_receipt(ctx("alice"), f.receipt_id)
    clean = not any(x in blob or x in repr(receipt) for x in ("October", "blue-green", "canary", "Weekly summary"))
    steps.append(
        Step(
            "audit receipts are content-free",
            clean and bool(audit["policy_decisions"]),
            {
                "policy_decisions": len(audit["policy_decisions"]),
                "release_audits": len(audit["release_audits"]),
                "tombstones": len(audit["deletion_tombstones"]),
            },
        )
    )
    return res


def run_partial_disclosure(
    service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock, tenant: str = "demo-availability"
) -> ScenarioResult:
    s = service
    steps: list[Step] = []
    res = ScenarioResult("partial-disclosure", steps)

    def ctx(who: str) -> Any:
        return auth.context(tenant, who)

    s.create_tenant(tenant, "Availability demo tenant")
    for pid, roles in (
        ("dana", ["engineer"]),
        ("paula", ["project_manager"]),
        ("bob", ["engineer"]),
        ("henry", ["hr"]),
        ("carol", ["manager"]),
    ):
        s.create_principal(tenant, pid, pid.title(), roles=roles)
    for pid in ("dana", "paula", "bob"):
        s.add_relationship_admin(tenant, pid, "member_of", "project", "apollo")
    r = s.commit_operations(
        ctx("dana"),
        [
            AddFact(
                fact=NewFact(
                    semantic_type=SemanticType.AVAILABILITY,
                    content_schema="availability",
                    content=Content(
                        fields={
                            "employee": "Dana Reyes",
                            "availability": "unavailable",
                            "return_date": "2026-10-20",
                            "reason": LEAVE_REASON,
                            "team": "apollo",
                        }
                    ),
                    topic="Dana availability update",
                    attribute="availability",
                    subject_ids=("dana",),
                    sensitivity=Sensitivity.CONFIDENTIAL,
                    policy_template=PolicyTemplateRef(name="availability_record", project_id="apollo"),
                )
            )
        ],
    )
    mid = r.results[0].memory_id or ""
    res.ids["availability"] = mid
    steps.append(Step("dana records private availability", r.status == "committed"))
    clock.advance(timedelta(minutes=1))

    expected = {"dana": View.FULL, "henry": View.FULL, "paula": View.REDACTED, "bob": View.AGGREGATE}
    for who, view in expected.items():
        m = s.get_memory(ctx(who), mid)
        leaked = LEAVE_REASON in m.text and view is not View.FULL
        steps.append(
            Step(
                f"{who} receives {view.value} view",
                m.view is view and not leaked,
                {"view": m.view.value, "fields": sorted(m.fields)},
            )
        )
    q = s.query(ctx("paula"), QueryRequest(query="Is Dana Reyes unavailable? What is the medical reason?"))
    steps.append(
        Step(
            "project manager query is ANSWER_REDACTED without the reason",
            q.action is QueryAction.ANSWER_REDACTED and "surgery" not in q.answer,
            {"action": q.action.value},
        )
    )
    qb = s.query(ctx("bob"), QueryRequest(query="How many team members are unavailable?"))
    steps.append(
        Step(
            "teammate sees only an aggregate",
            qb.action is QueryAction.ANSWER_REDACTED
            and "Dana" not in qb.answer
            and all(e.memory_id is None for e in qb.evidence),
            {"action": qb.action.value},
        )
    )
    qc = s.query(ctx("carol"), QueryRequest(query="Is Dana Reyes unavailable?"))
    steps.append(
        Step("unrelated manager gets NO_MEMORY", qc.action is QueryAction.NO_MEMORY, {"action": qc.action.value})
    )
    return res


def run_all(service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock) -> list[ScenarioResult]:
    return [run_apollo_lifecycle(service, auth, clock), run_partial_disclosure(service, auth, clock)]
