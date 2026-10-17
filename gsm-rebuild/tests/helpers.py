"""Scenario helpers shared by the test suites."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from gsm.auth import TrustedAuthenticator
from gsm.clock import FrozenClock
from gsm.domain.content import Content
from gsm.domain.context import PrincipalContext
from gsm.domain.operations import AddFact, NewFact, PolicyTemplateRef
from gsm.domain.types import AuthStrength, SemanticType, Sensitivity
from gsm.service import GovernedMemoryService

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


@dataclass
class World:
    """Acme tenant with the Apollo project.

    alice  engineer, owner_of + member_of project:apollo
    bob    engineer, member_of project:apollo
    carol  manager, outside Apollo
    dana   engineer, member_of project:apollo (subject of a private availability record)
    paula  project_manager, member_of project:apollo
    henry  hr
    mallory contractor, outside Apollo
    """

    service: GovernedMemoryService
    auth: TrustedAuthenticator
    clock: FrozenClock
    tenant: str = "acme"

    def ctx(
        self,
        who: str,
        *,
        strength: AuthStrength = AuthStrength.PASSWORD,
        purpose: str | None = None,
        tenant: str | None = None,
    ) -> PrincipalContext:
        return self.auth.context(tenant or self.tenant, who, auth_strength=strength, purpose=purpose)

    def tick(self, minutes: int = 1) -> None:
        self.clock.advance(timedelta(minutes=minutes))


def build_world(service: GovernedMemoryService, auth: TrustedAuthenticator, clock: FrozenClock) -> World:
    s = service
    s.create_tenant("acme", "Acme Corp")
    for pid, name, roles in [
        ("alice", "Alice", ["engineer"]),
        ("bob", "Bob", ["engineer"]),
        ("carol", "Carol", ["manager"]),
        ("dana", "Dana", ["engineer"]),
        ("paula", "Paula", ["project_manager"]),
        ("henry", "Henry", ["hr"]),
        ("mallory", "Mallory", ["contractor"]),
    ]:
        s.create_principal("acme", pid, name, roles=roles)
    s.add_relationship_admin("acme", "alice", "owner_of", "project", "apollo")
    for pid in ("alice", "bob", "dana", "paula"):
        s.add_relationship_admin("acme", pid, "member_of", "project", "apollo")
    s.add_relationship_admin("acme", "carol", "manager_of", "principal", "mallory")
    return World(service=s, auth=auth, clock=clock)


def apollo_decision(text_value: str = "Launch Apollo on October 14 with the blue-green rollout") -> AddFact:
    return AddFact(
        fact=NewFact(
            semantic_type=SemanticType.DECISION,
            content_schema="note",
            content=Content(text=text_value),
            topic="Apollo launch decision",
            attribute="launch_plan",
            subject_ids=("project:apollo",),
            sensitivity=Sensitivity.CONFIDENTIAL,
            policy_template=PolicyTemplateRef(name="project", project_id="apollo"),
        )
    )


def availability_fact(reason: str = "knee surgery recovery") -> AddFact:
    """Dana's private availability record (the partial-disclosure scenario)."""
    return AddFact(
        fact=NewFact(
            semantic_type=SemanticType.AVAILABILITY,
            content_schema="availability",
            content=Content(
                fields={
                    "employee": "Dana Reyes",
                    "availability": "unavailable",
                    "return_date": "2026-10-20",
                    "reason": reason,
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


def note_fact(
    text_value: str,
    topic: str,
    *,
    template: str = "private",
    project_id: str | None = None,
    subjects: tuple[str, ...] = (),
    compartments: frozenset[str] = frozenset(),
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    attribute: str | None = None,
) -> AddFact:
    return AddFact(
        fact=NewFact(
            semantic_type=SemanticType.FACT,
            content=Content(text=text_value),
            topic=topic,
            attribute=attribute,
            subject_ids=subjects,
            sensitivity=sensitivity,
            compartments=compartments,
            policy_template=PolicyTemplateRef(name=template, project_id=project_id),  # type: ignore[arg-type]
        )
    )


def add(world: World, who: str, op: AddFact) -> str:
    receipt = world.service.commit_operations(world.ctx(who), [op])
    assert receipt.status == "committed", receipt
    mid = receipt.results[0].memory_id
    assert mid is not None
    return mid
