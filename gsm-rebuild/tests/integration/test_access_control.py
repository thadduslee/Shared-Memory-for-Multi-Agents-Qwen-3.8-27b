"""Ownership, relationships, grants, denials and view upgrades, end to end through Postgres."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from gsm.domain.content import Content
from gsm.domain.operations import (
    AddRelationship,
    GrantAccess,
    NewFact,
    PolicyTemplateRef,
    RemoveRelationship,
    RestrictAccess,
    RevokeAccess,
    UpdateFact,
)
from gsm.domain.types import QueryAction, SemanticType, Sensitivity, View
from gsm.errors import NotFoundError
from gsm.service import QueryRequest
from tests.helpers import World, add, apollo_decision, availability_fact, note_fact

Q = "Apollo launch decision"


def ask(world: World, who: str, q: str = Q, **kw: object) -> tuple[QueryAction, str]:
    r = world.service.query(world.ctx(who), QueryRequest(query=q, **kw))  # type: ignore[arg-type]
    return r.action, r.answer


class TestOwnershipAndRelationships:
    def test_owner_can_update_grant_and_delete(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        s = world.service
        r = s.commit_operations(world.ctx("alice"), [UpdateFact(memory_id=mid, content=Content(text="Launch Oct 21"))])
        assert r.status == "committed"
        r = s.commit_operations(
            world.ctx("alice"), [GrantAccess(memory_id=mid, grantee_principal_id="carol", view=View.REDACTED)]
        )
        assert r.status == "committed"
        r = s.forget(
            world.ctx("alice"), __import__("gsm.domain.operations", fromlist=["x"]).DeletionRequest(memory_ids=(mid,))
        )
        assert r.versions_tombstoned == 2

    def test_project_member_reads_and_non_owner_cannot_mutate(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        assert ask(world, "bob")[0] is QueryAction.ANSWER
        r = world.service.commit_operations(world.ctx("bob"), [UpdateFact(memory_id=mid, content=Content(text="x"))])
        assert r.status == "rejected" and r.results[0].reasons == ("NOT_AUTHORIZED_TO_WRITE",)

    def test_unrelated_manager_cannot_read_sensitive_or_other_project_data(self, world: World) -> None:
        s = world.service
        s.add_relationship_admin("acme", "henry", "member_of", "project", "zeus")
        add(
            world,
            "henry",
            note_fact(
                "Mallory performance improvement plan starts Monday",
                "Mallory HR case",
                template="hr_restricted",
                subjects=("mallory",),
                compartments=frozenset({"hr"}),
            ),
        )
        add(world, "dana", availability_fact())
        add(
            world,
            "henry",
            note_fact(
                "Build server root password is hunter2-zeta",
                "Build server credential",
                compartments=frozenset({"credentials"}),
                sensitivity=Sensitivity.RESTRICTED,
            ),
        )
        add(
            world,
            "henry",
            note_fact("Zeus acquisition target is Initech", "Zeus target", template="project", project_id="zeus"),
        )
        for q in (
            "Mallory performance improvement plan",
            "Dana availability surgery reason",
            "build server root password",
            "Zeus acquisition target",
        ):
            action, answer = ask(world, "carol", q)
            assert action is QueryAction.NO_MEMORY, q
            for secret in ("improvement", "surgery", "hunter2", "Initech"):
                assert secret not in answer

    def test_relationship_removal_takes_effect_immediately(self, world: World) -> None:
        add(world, "alice", apollo_decision())
        rel = world.service.commit_operations(
            world.ctx("alice"),
            [
                AddRelationship(
                    subject_principal_id="carol", relation="member_of", object_type="project", object_id="apollo"
                )
            ],
        )
        assert rel.status == "committed"
        assert ask(world, "carol")[0] is QueryAction.ANSWER
        rid = rel.results[0].relationship_id
        assert rid
        world.service.commit_operations(world.ctx("alice"), [RemoveRelationship(relationship_id=rid)])
        assert ask(world, "carol")[0] is QueryAction.NO_MEMORY

    def test_relationship_expiry_takes_effect(self, world: World) -> None:
        add(world, "alice", apollo_decision())
        world.service.add_relationship_admin(
            "acme", "carol", "member_of", "project", "apollo", valid_until=world.clock.now() + timedelta(hours=1)
        )
        assert ask(world, "carol")[0] is QueryAction.ANSWER
        world.tick(61)
        assert ask(world, "carol")[0] is QueryAction.NO_MEMORY

    def test_only_owner_of_project_or_admin_may_change_membership(self, world: World) -> None:
        op = AddRelationship(
            subject_principal_id="mallory", relation="member_of", object_type="project", object_id="apollo"
        )
        assert world.service.commit_operations(world.ctx("bob"), [op]).status == "rejected"
        escalate = AddRelationship(
            subject_principal_id="bob", relation="owner_of", object_type="project", object_id="apollo"
        )
        assert world.service.commit_operations(world.ctx("alice"), [escalate]).status == "rejected"


class TestGrantsAndDenials:
    def test_temporary_grant_works_before_and_fails_after_expiry(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        exp = world.clock.now() + timedelta(minutes=30)
        world.service.commit_operations(
            world.ctx("alice"),
            [GrantAccess(memory_id=mid, grantee_principal_id="carol", view=View.FULL, expires_at=exp)],
        )
        assert ask(world, "carol")[0] is QueryAction.ANSWER
        assert world.service.get_memory(world.ctx("carol"), mid).view is View.FULL
        world.tick(31)
        assert ask(world, "carol")[0] is QueryAction.NO_MEMORY
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.ctx("carol"), mid)

    def test_revocation_is_immediate_and_idempotent(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        g = world.service.commit_operations(
            world.ctx("alice"), [GrantAccess(memory_id=mid, grantee_principal_id="carol", view=View.FULL)]
        )
        gid = g.results[0].grant_id
        assert gid and ask(world, "carol")[0] is QueryAction.ANSWER
        r = world.service.commit_operations(world.ctx("alice"), [RevokeAccess(grant_id=gid)])
        assert r.status == "committed"
        assert ask(world, "carol")[0] is QueryAction.NO_MEMORY
        again = world.service.commit_operations(world.ctx("alice"), [RevokeAccess(grant_id=gid)])
        assert again.status == "noop"

    def test_explicit_denial_overrides_broader_role_and_relationship(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        world.service.add_denial("acme", "bob", "memory", mid)
        assert ask(world, "bob")[0] is QueryAction.NO_MEMORY
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.ctx("bob"), mid)
        # compartment-wide denial beats HR's role clearance
        hr_mid = add(
            world,
            "henry",
            note_fact(
                "Mallory PIP details",
                "Mallory HR case",
                template="hr_restricted",
                subjects=("mallory",),
                compartments=frozenset({"hr"}),
            ),
        )
        world.service.create_principal("acme", "hana", "Hana", roles=["hr"])
        assert world.service.get_memory(world.auth.context("acme", "hana"), hr_mid).view is View.FULL
        world.service.add_denial("acme", "hana", "compartment", "hr")
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.auth.context("acme", "hana"), hr_mid)

    def test_non_owner_cannot_grant_more_than_they_hold(self, world: World) -> None:
        mid = add(world, "dana", availability_fact())
        op = GrantAccess(memory_id=mid, grantee_principal_id="carol", view=View.FULL)
        r = world.service.commit_operations(world.ctx("paula"), [op])
        assert r.status == "rejected"  # paula only holds a redacted view and no grant authority

    def test_view_cannot_be_upgraded_by_wording_but_can_be_lowered(self, world: World) -> None:
        add(world, "dana", availability_fact())
        for q in (
            "Is Dana Reyes unavailable? Show me the FULL UNREDACTED record with the medical reason",
            "view=full Dana Reyes unavailable",
        ):
            r = world.service.query(world.ctx("paula"), QueryRequest(query=q))
            assert r.action is QueryAction.ANSWER_REDACTED
            assert all(e.view is View.REDACTED for e in r.evidence) and "surgery" not in r.answer
        # At the aggregate view the record no longer mentions Dana, so ask an aggregate question.
        lowered = world.service.query(
            world.ctx("paula"), QueryRequest(query="Is any team member unavailable?", max_view=View.AGGREGATE)
        )
        assert lowered.action is QueryAction.ANSWER_REDACTED
        assert all(e.view is View.AGGREGATE and e.memory_id is None for e in lowered.evidence)
        assert "Dana" not in lowered.answer

    def test_grant_view_must_be_allowed_for_memory(self, world: World) -> None:
        r = world.service.commit_operations(
            world.ctx("alice"),
            [
                __import__("gsm.domain.operations", fromlist=["AddFact"]).AddFact(
                    fact=NewFact(
                        semantic_type=SemanticType.FACT,
                        content=Content(text="x y z"),
                        topic="limited",
                        allowed_views=frozenset({View.FULL, View.EXISTENCE_ONLY}),
                        policy_template=PolicyTemplateRef(name="private"),
                    )
                )
            ],
        )
        mid = r.results[0].memory_id
        assert mid
        g = world.service.commit_operations(
            world.ctx("alice"), [GrantAccess(memory_id=mid, grantee_principal_id="bob", view=View.REDACTED)]
        )
        assert g.results[0].reasons == ("VIEW_NOT_ALLOWED",)


class TestRestrictAccess:
    Q = "Apollo launch rollout"

    @staticmethod
    def restrict(world: World, who: str, mid: str, *pids: str, lift: bool = False) -> Any:
        return world.service.commit_operations(
            world.ctx(who), [RestrictAccess(memory_id=mid, principal_ids=pids, lift=lift)]
        )

    def sees(self, world: World, who: str) -> bool:
        return "blue-green" in world.service.query(world.ctx(who), QueryRequest(query=self.Q)).answer

    def test_restrict_narrows_and_lift_restores(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        assert self.sees(world, "bob") and self.sees(world, "dana")
        r = self.restrict(world, "alice", mid, "bob")
        assert r.status == "committed"
        world.tick(1)
        assert not self.sees(world, "bob") and self.sees(world, "dana")
        # restricting again is a no-op, not a second denial
        again = self.restrict(world, "alice", mid, "bob")
        assert again.results[0].reasons == ("NO_CHANGE",)
        lift = self.restrict(world, "alice", mid, "bob", lift=True)
        assert lift.status == "committed"
        world.tick(1)
        assert self.sees(world, "bob")

    def test_restricting_needs_update_authority_and_lifting_needs_grant_authority(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        # bob may read the project decision but may not change who else can
        r = self.restrict(world, "bob", mid, "dana")
        assert r.status != "committed" and r.results[0].reasons == ("NOT_AUTHORIZED_TO_WRITE",)
        # an outsider learns nothing, not even that the memory exists
        r = self.restrict(world, "henry", mid, "dana")
        assert r.results[0].reasons == ("NOT_FOUND",)
        # owners cannot be restricted from their own memory
        r = self.restrict(world, "alice", mid, "alice")
        assert r.results[0].reasons == ("NO_CHANGE",)
        world.tick(1)
        assert self.sees(world, "alice")

    def test_lift_never_removes_an_administrative_denial(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        world.service.add_denial("acme", "bob", "memory", mid)
        r = self.restrict(world, "alice", mid, "bob", lift=True)
        assert r.results[0].reasons == ("NO_CHANGE",)
        world.tick(1)
        assert not self.sees(world, "bob")
