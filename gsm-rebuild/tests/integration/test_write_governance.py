"""Quarantine of model proposals, declassification, and derived-memory policy inheritance."""

from __future__ import annotations

import pytest

from gsm.domain.content import Content
from gsm.domain.operations import (
    AddFact,
    DeleteFact,
    DeriveRequest,
    GrantAccess,
    NewFact,
    PolicyTemplateRef,
    UpdateFact,
)
from gsm.domain.policy import MemoryPolicy
from gsm.domain.types import Origin, QueryAction, SemanticType, Sensitivity, View
from gsm.errors import ForbiddenError, NotFoundError
from gsm.policy import templates
from gsm.service import QueryRequest
from tests.helpers import World, add, apollo_decision, availability_fact, note_fact


class TestQuarantine:
    def test_model_high_impact_ops_are_quarantined(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        ops = [
            DeleteFact(memory_ids=(mid,)),
            GrantAccess(memory_id=mid, grantee_principal_id="carol", view=View.FULL),
        ]
        r = world.service.commit_operations(world.ctx("alice"), ops, origin=Origin.MODEL)
        assert r.status == "partial_quarantine"
        assert all(x.status == "quarantined" for x in r.results)
        assert "HIGH_IMPACT_REQUIRES_CONFIRMATION" in r.results[0].reasons
        assert world.service.get_memory(world.ctx("alice"), mid)  # nothing was deleted
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.ctx("carol"), mid)  # nothing was granted

    def test_model_cannot_author_policy_owners_or_reclassify(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        authored = AddFact(
            fact=NewFact(
                semantic_type=SemanticType.NOTE, content=Content(text="x"), topic="t", policy=templates.tenant_public()
            )
        )
        owners = AddFact(
            fact=NewFact(
                semantic_type=SemanticType.NOTE, content=Content(text="x"), topic="t", owner_ids=("alice", "mallory")
            )
        )
        downgrade = UpdateFact(memory_id=mid, content=Content(text="y"), sensitivity=Sensitivity.PUBLIC)
        r = world.service.commit_operations(world.ctx("alice"), [authored, owners, downgrade], origin=Origin.MODEL)
        assert [x.status for x in r.results] == ["quarantined"] * 3

    def test_model_low_risk_add_commits_with_forced_ownership(self, world: World) -> None:
        op = AddFact(
            fact=NewFact(
                semantic_type=SemanticType.NOTE,
                content=Content(text="Standup at ten"),
                topic="standup",
                policy_template=PolicyTemplateRef(name="private"),
            ),
            confidence=0.9,
        )
        r = world.service.commit_operations(world.ctx("bob"), [op], origin=Origin.MODEL)
        assert r.status == "committed"

    def test_low_confidence_changes_to_sensitive_records_are_quarantined(self, world: World) -> None:
        low = note_fact("Maybe Dana is leaving?", "Dana rumor", sensitivity=Sensitivity.CONFIDENTIAL)
        low = low.model_copy(update={"confidence": 0.4})
        r = world.service.commit_operations(world.ctx("alice"), [low])
        assert r.results[0].status == "quarantined" and r.results[0].reasons == ("LOW_CONFIDENCE_SENSITIVE",)
        mid = add(world, "dana", availability_fact())
        upd = UpdateFact(
            memory_id=mid,
            content=Content(
                fields={"employee": "Dana Reyes", "availability": "available", "return_date": "now", "reason": "none"}
            ),
            confidence=0.3,
        )
        assert world.service.commit_operations(world.ctx("dana"), [upd]).results[0].status == "quarantined"


class TestDeclassification:
    def test_broadening_requires_explicit_owner_declassification(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        widen = UpdateFact(memory_id=mid, content=Content(text="Launch Oct 14"), policy=templates.tenant_public())
        r = world.service.commit_operations(world.ctx("alice"), [widen])
        assert r.results[0].reasons == ("POLICY_BROADENING",)
        down = UpdateFact(memory_id=mid, content=Content(text="Launch Oct 14"), sensitivity=Sensitivity.PUBLIC)
        assert world.service.commit_operations(world.ctx("alice"), [down]).results[0].reasons == (
            "SENSITIVITY_DOWNGRADE",
        )
        ok = world.service.commit_operations(world.ctx("alice"), [widen.model_copy(update={"declassify": True})])
        assert ok.status == "committed" and "DECLASSIFIED" in ok.results[0].reasons
        world.tick()
        assert world.service.query(world.ctx("carol"), QueryRequest(query="launch Oct 14")).action is QueryAction.ANSWER

    def test_tightening_needs_no_declassification(self, world: World) -> None:
        mid = add(world, "alice", apollo_decision())
        tighten = UpdateFact(
            memory_id=mid,
            content=Content(text="Launch Oct 14"),
            allowed_views=frozenset({View.FULL}),
            sensitivity=Sensitivity.RESTRICTED,
        )
        assert world.service.commit_operations(world.ctx("alice"), [tighten]).status == "committed"


class TestDerivation:
    def test_derivation_requires_full_view_of_every_parent(self, world: World) -> None:
        leave = add(world, "dana", availability_fact())
        with pytest.raises(ForbiddenError):
            world.service.derive(
                world.ctx("paula"),
                DeriveRequest(
                    parent_ids=(leave,), content=Content(text="Dana out until Oct 20"), topic="team availability"
                ),
            )
        with pytest.raises(NotFoundError):
            world.service.derive(
                world.ctx("carol"), DeriveRequest(parent_ids=(leave,), content=Content(text="x"), topic="t")
            )

    def test_derived_memory_is_as_restrictive_as_every_parent(self, world: World) -> None:
        decision = add(world, "alice", apollo_decision())
        leave = add(world, "dana", availability_fact())
        d = world.service.derive(
            world.ctx("bob"),
            DeriveRequest(
                parent_ids=(decision,), content=Content(text="Apollo launch staffing summary"), topic="staffing"
            ),
        )
        assert d.inherits_parent_policy
        assert world.service.get_memory(world.ctx("paula"), d.memory_id).view is View.FULL
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.ctx("carol"), d.memory_id)
        # bob only holds the aggregate view of dana's record: he cannot summarize it
        with pytest.raises(ForbiddenError):
            world.service.derive(
                world.ctx("bob"),
                DeriveRequest(
                    parent_ids=(decision, leave),
                    content=Content(text="Apollo staffing: one engineer out"),
                    topic="Apollo staffing",
                ),
            )

    def test_summary_visibility_tracks_parents_live(self, world: World) -> None:
        a = add(world, "alice", apollo_decision())
        world.service.add_relationship_admin("acme", "alice", "member_of", "project", "zeus")
        z = add(
            world, "alice", note_fact("Zeus launch slips a week", "Zeus launch", template="project", project_id="zeus")
        )
        s = world.service.derive(
            world.ctx("alice"),
            DeriveRequest(
                parent_ids=(a, z),
                content=Content(text="Portfolio summary: Apollo Oct 14, Zeus slips a week"),
                topic="Portfolio summary",
            ),
        )
        # bob is in Apollo but not Zeus: the summary is hidden from him
        with pytest.raises(NotFoundError):
            world.service.get_memory(world.ctx("bob"), s.memory_id)
        world.service.add_relationship_admin("acme", "bob", "member_of", "project", "zeus")
        assert "Portfolio" in world.service.get_memory(world.ctx("bob"), s.memory_id).text

    def test_parent_without_full_view_cannot_be_summarized(self, world: World) -> None:
        redacted_only = add(
            world,
            "alice",
            AddFact(
                fact=NewFact(
                    semantic_type=SemanticType.FACT,
                    content=Content(text="beta"),
                    topic="beta",
                    allowed_views=frozenset({View.REDACTED}),
                )
            ),
        )
        with pytest.raises(ForbiddenError):
            world.service.derive(
                world.ctx("alice"), DeriveRequest(parent_ids=(redacted_only,), content=Content(text="x"), topic="x")
            )

    def test_meet_rejects_conflicting_parents(self) -> None:
        from gsm.domain.policy import MandatoryConstraints
        from gsm.memory.errors import OpRejected
        from gsm.memory.provenance import meet

        def parent(
            views: list[str], purposes: frozenset[str] = frozenset(), sens: str = "internal"
        ) -> dict[str, object]:
            policy = MemoryPolicy(mandatory=MandatoryConstraints(purposes=purposes))
            return {
                "sensitivity": sens,
                "compartments": ["hr"] if sens == "restricted" else [],
                "allowed_views": views,
                "subject_ids": ["x"],
                "owner_ids": ["a"],
                "policy": policy.model_dump(),
            }

        with pytest.raises(OpRejected):
            meet([parent(["full"]), parent(["redacted"])])
        with pytest.raises(OpRejected):
            meet([parent(["full"], frozenset({"audit"})), parent(["full"], frozenset({"payroll"}))])
        m = meet(
            [
                parent(["full", "redacted"], frozenset({"audit", "payroll"})),
                parent(["full", "aggregate"], frozenset({"payroll"}), sens="restricted"),
            ]
        )
        assert m.allowed_views == frozenset({View.FULL}) and m.sensitivity is Sensitivity.RESTRICTED
        assert m.compartments == frozenset({"hr"}) and m.mandatory.purposes == frozenset({"payroll"})

    def test_declassification_only_by_owner_of_all_parents(self, world: World) -> None:
        a = add(world, "alice", apollo_decision())
        req = DeriveRequest(
            parent_ids=(a,),
            content=Content(text="Public: Apollo launches in October"),
            topic="Apollo public note",
            declassification=templates.tenant_public(),
        )
        world.service.add_relationship_admin("acme", "carol", "member_of", "project", "apollo")
        with pytest.raises(ForbiddenError):
            world.service.derive(world.ctx("carol"), req)
        with pytest.raises(ForbiddenError):
            world.service.derive(world.ctx("alice"), req, origin=Origin.MODEL)
        d = world.service.derive(world.ctx("alice"), req)
        assert not d.inherits_parent_policy and "DECLASSIFIED" in d.reason_codes
        world.tick()
        assert "October" in world.service.get_memory(world.ctx("mallory"), d.memory_id).text

    def test_summaries_cannot_be_added_without_provenance(self) -> None:
        with pytest.raises(ValueError):
            NewFact(semantic_type=SemanticType.SUMMARY, content=Content(text="x"), topic="x", policy=MemoryPolicy())
