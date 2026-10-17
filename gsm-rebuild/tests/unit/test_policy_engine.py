"""Deterministic policy engine: precedence, RBAC/ReBAC/ABAC, grants, denials, lineage."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gsm.domain.context import PrincipalContext
from gsm.domain.policy import (
    AccessRule,
    MandatoryConstraints,
    MemoryPolicy,
    PrincipalPredicate,
    RelationRequirement,
)
from gsm.domain.reasons import Reason
from gsm.domain.types import AuthStrength, Decision, LifecycleState, Sensitivity, VersionState, View
from gsm.policy import templates
from gsm.policy.engine import PolicyEngine, WriteAction
from gsm.policy.facts import AuthSnapshot, DenialFact, GrantFact, MemoryFacts, RelationFact, VersionFacts

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ALL_VIEWS = frozenset(View)
engine = PolicyEngine()


def ctx(
    pid: str = "bob",
    *,
    roles: tuple[str, ...] = (),
    strength: AuthStrength = AuthStrength.PASSWORD,
    purpose: str | None = None,
    tenant: str = "acme",
    workspace: str = "default",
) -> PrincipalContext:
    return PrincipalContext(
        tenant_id=tenant,
        workspace_id=workspace,
        principal_id=pid,
        roles=frozenset(roles),
        auth_time=NOW,
        auth_strength=strength,
        purpose=purpose,
        request_time=NOW,
        correlation_id="corr_test_0001",
        issued_by="test",
    )


def snap(
    pid: str = "bob",
    *,
    relations: tuple[tuple[str, str, str], ...] = (),
    grants: tuple[GrantFact, ...] = (),
    compartment_grants: tuple[GrantFact, ...] = (),
    denials: tuple[DenialFact, ...] = (),
    active: bool = True,
    tenant: str = "acme",
) -> AuthSnapshot:
    mg: dict[str, tuple[GrantFact, ...]] = {}
    for g in grants:
        assert g.memory_id
        mg[g.memory_id] = (*mg.get(g.memory_id, ()), g)
    return AuthSnapshot(
        tenant_id=tenant,
        principal_id=pid,
        principal_active=active,
        taken_at=NOW,
        relations=tuple(RelationFact(r, t, o, NOW - timedelta(days=1)) for r, t, o in relations),
        memory_grants=mg,
        compartment_grants=compartment_grants,
        denials=denials,
    )


def facts(policy: MemoryPolicy | None = None, **kw: object) -> MemoryFacts:
    base = MemoryFacts(
        memory_id="mem_1",
        tenant_id="acme",
        workspace_id="default",
        lifecycle_state=LifecycleState.ACTIVE,
        retain_until=None,
        owner_ids=frozenset({"alice"}),
        subject_ids=frozenset({"project:apollo"}),
        sensitivity=Sensitivity.INTERNAL,
        compartments=frozenset(),
        policy=policy or templates.project("apollo"),
        policy_revision=1,
        allowed_views=ALL_VIEWS,
        version=VersionFacts("ver_1", VersionState.ACTIVE, NOW - timedelta(hours=1), None),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


MEMBER = (("member_of", "project", "apollo"),)


def grant(
    view: View = View.FULL,
    *,
    expires: datetime | None = None,
    revoked: datetime | None = None,
    purposes: frozenset[str] = frozenset(),
) -> GrantFact:
    return GrantFact("grant_1", "mem_1", None, view, purposes, NOW - timedelta(hours=1), expires, revoked, "alice")


class TestPrecedence:
    def test_project_member_allowed_full(self) -> None:
        d = engine.evaluate_read(ctx(), facts(), snap(relations=MEMBER))
        assert d.decision is Decision.ALLOW and d.view is View.FULL
        assert Reason.RELATIONSHIP_RULE in d.reasons

    def test_outsider_denied_without_existence_ack(self) -> None:
        d = engine.evaluate_read(ctx("carol", roles=("manager",)), facts(), snap("carol"))
        assert d.decision is Decision.DENY and not d.existence_ack and d.view is None

    def test_tenant_mismatch_beats_everything(self) -> None:
        d = engine.evaluate_read(ctx("alice", tenant="other"), facts(), snap("alice", relations=MEMBER, tenant="other"))
        assert d.decision is Decision.DENY and d.reasons == (Reason.TENANT_MISMATCH,)

    def test_workspace_mismatch(self) -> None:
        d = engine.evaluate_read(ctx("alice", workspace="ws2"), facts(), snap("alice"))
        assert d.reasons == (Reason.WORKSPACE_MISMATCH,)

    def test_snapshot_for_other_principal_is_rejected(self) -> None:
        d = engine.evaluate_read(ctx("bob"), facts(), snap("alice", relations=MEMBER))
        assert d.decision is Decision.DENY and Reason.CONTEXT_UNTRUSTED in d.reasons

    def test_inactive_principal(self) -> None:
        d = engine.evaluate_read(ctx("alice"), facts(), snap("alice", active=False))
        assert d.reasons == (Reason.PRINCIPAL_INACTIVE,)

    @pytest.mark.parametrize("state", [LifecycleState.TOMBSTONED, LifecycleState.PENDING_INVALIDATION])
    def test_tombstone_and_pending_are_unavailable_even_for_owner(self, state: LifecycleState) -> None:
        d = engine.evaluate_read(ctx("alice"), facts(lifecycle_state=state), snap("alice", relations=MEMBER))
        assert d.decision is Decision.UNAVAILABLE

    def test_tombstoned_version_is_unavailable(self) -> None:
        v = VersionFacts("ver_1", VersionState.TOMBSTONED, NOW - timedelta(hours=1), None)
        d = engine.evaluate_read(ctx("alice"), facts(version=v), snap("alice"))
        assert d.decision is Decision.UNAVAILABLE and Reason.TOMBSTONED in d.reasons

    def test_retention_expiry(self) -> None:
        d = engine.evaluate_read(ctx("alice"), facts(retain_until=NOW - timedelta(seconds=1)), snap("alice"))
        assert d.decision is Decision.UNAVAILABLE and Reason.EXPIRED in d.reasons

    def test_version_not_yet_valid_and_expired(self) -> None:
        future = VersionFacts("ver_1", VersionState.ACTIVE, NOW + timedelta(days=1), None)
        assert Reason.NOT_YET_VALID in engine.evaluate_read(ctx("alice"), facts(version=future), snap("alice")).reasons
        past = VersionFacts("ver_1", VersionState.ACTIVE, NOW - timedelta(days=2), NOW - timedelta(days=1))
        assert Reason.EXPIRED in engine.evaluate_read(ctx("alice"), facts(version=past), snap("alice")).reasons

    def test_malformed_policy_fails_closed(self) -> None:
        d = engine.evaluate_read(ctx("alice"), replace(facts(), policy=None), snap("alice"))
        assert d.decision is Decision.DENY and Reason.MALFORMED_POLICY in d.reasons

    def test_explicit_denial_overrides_owner_grant_and_rule(self) -> None:
        denial = DenialFact("deny_1", "memory", "mem_1", NOW - timedelta(hours=1), None, None)
        d = engine.evaluate_read(
            ctx("alice"), facts(), snap("alice", relations=MEMBER, grants=(grant(),), denials=(denial,))
        )
        assert d.decision is Decision.DENY and d.reasons == (Reason.EXPLICIT_DENIAL,) and not d.existence_ack

    def test_subject_and_compartment_denials(self) -> None:
        for denial in (
            DenialFact("d", "subject", "project:apollo", NOW - timedelta(hours=1), None, None),
            DenialFact("d", "compartment", "hr", NOW - timedelta(hours=1), None, None),
        ):
            f = facts(compartments=frozenset({"hr"}))
            d = engine.evaluate_read(ctx(roles=("hr",)), f, snap(relations=MEMBER, denials=(denial,)))
            assert d.reasons == (Reason.EXPLICIT_DENIAL,)

    def test_lifted_or_expired_denial_no_longer_applies(self) -> None:
        lifted = DenialFact("d", "memory", "mem_1", NOW - timedelta(hours=2), None, NOW - timedelta(hours=1))
        expired = DenialFact("d", "memory", "mem_1", NOW - timedelta(hours=2), NOW - timedelta(hours=1), None)
        for denial in (lifted, expired):
            assert engine.evaluate_read(ctx(), facts(), snap(relations=MEMBER, denials=(denial,))).allowed

    def test_owner_access(self) -> None:
        d = engine.evaluate_read(ctx("alice"), facts(), snap("alice"))
        assert d.decision is Decision.ALLOW and Reason.OWNER_ACCESS in d.reasons

    def test_owner_view_none_removes_owner_privilege(self) -> None:
        d = engine.evaluate_read(ctx("alice"), facts(MemoryPolicy(owner_view=None)), snap("alice"))
        assert d.decision is Decision.DENY

    def test_mandatory_constraints_bind_owners(self) -> None:
        policy = MemoryPolicy(mandatory=MandatoryConstraints(purposes=frozenset({"payroll"})))
        d = engine.evaluate_read(ctx("alice", purpose="marketing"), facts(policy), snap("alice"))
        assert d.decision is Decision.DENY and Reason.PURPOSE_NOT_PERMITTED in d.reasons

    def test_missing_purpose_escalates_only_if_otherwise_allowed(self) -> None:
        policy = MemoryPolicy(mandatory=MandatoryConstraints(purposes=frozenset({"payroll"})))
        owner = engine.evaluate_read(ctx("alice"), facts(policy), snap("alice"))
        assert owner.decision is Decision.ESCALATE and Reason.PURPOSE_MISSING in owner.reasons
        outsider = engine.evaluate_read(ctx("carol"), facts(policy), snap("carol"))
        assert outsider.decision is Decision.DENY  # never reveal via ESCALATE

    def test_sensitivity_requires_stronger_auth(self) -> None:
        f = facts(sensitivity=Sensitivity.RESTRICTED)
        weak = engine.evaluate_read(ctx(), f, snap(relations=MEMBER))
        assert weak.decision is Decision.ESCALATE and Reason.AUTH_STRENGTH_INSUFFICIENT in weak.reasons
        strong = engine.evaluate_read(ctx(strength=AuthStrength.MFA), f, snap(relations=MEMBER))
        assert strong.decision is Decision.ALLOW


class TestGrants:
    def test_grant_gives_its_view(self) -> None:
        d = engine.evaluate_read(ctx("carol"), facts(), snap("carol", grants=(grant(View.REDACTED),)))
        assert d.decision is Decision.ALLOW_REDACTED and d.view is View.REDACTED
        assert Reason.EXPLICIT_GRANT in d.reasons

    def test_grant_expiry(self) -> None:
        g = grant(expires=NOW + timedelta(minutes=5))
        assert engine.evaluate_read(ctx("carol"), facts(), snap("carol", grants=(g,))).allowed
        later = ctx("carol").model_copy(update={"request_time": NOW + timedelta(minutes=6)})
        assert not engine.evaluate_read(later, facts(), snap("carol", grants=(g,))).allowed

    def test_revoked_grant(self) -> None:
        g = grant(revoked=NOW - timedelta(seconds=1))
        assert not engine.evaluate_read(ctx("carol"), facts(), snap("carol", grants=(g,))).allowed

    def test_grant_purpose_binding(self) -> None:
        g = grant(purposes=frozenset({"audit"}))
        assert not engine.evaluate_read(ctx("carol"), facts(), snap("carol", grants=(g,))).allowed
        assert engine.evaluate_read(ctx("carol", purpose="audit"), facts(), snap("carol", grants=(g,))).allowed

    def test_most_revealing_allow_path_wins(self) -> None:
        d = engine.evaluate_read(ctx(), facts(), snap(relations=MEMBER, grants=(grant(View.AGGREGATE),)))
        assert d.view is View.FULL


class TestRulesAndCompartments:
    def test_unrelated_manager_cannot_read_hr_or_credentials(self) -> None:
        for policy, comps in ((templates.hr_restricted(), {"hr"}), (templates.private(), {"credentials"})):
            f = facts(
                policy, subject_ids=frozenset({"dana"}), compartments=frozenset(comps), owner_ids=frozenset({"x"})
            )
            d = engine.evaluate_read(ctx("carol", roles=("manager",)), f, snap("carol"))
            assert d.decision is Decision.DENY and not d.existence_ack

    def test_role_rule_requires_compartment_clearance(self) -> None:
        policy = MemoryPolicy(rules=(AccessRule(any_roles=frozenset({"manager"}), view=View.FULL),))
        f = facts(policy, compartments=frozenset({"hr"}), owner_ids=frozenset())
        d = engine.evaluate_read(ctx("carol", roles=("manager",)), f, snap("carol"))
        assert d.decision is Decision.DENY and Reason.COMPARTMENT_NOT_CLEARED in d.reasons
        cleared = GrantFact("g", None, "hr", None, frozenset(), NOW - timedelta(hours=1), None, None, "admin")
        assert engine.evaluate_read(
            ctx("carol", roles=("manager",)), f, snap("carol", compartment_grants=(cleared,))
        ).allowed

    def test_hr_role_cleared_for_hr_compartment(self) -> None:
        f = facts(
            templates.hr_restricted(),
            subject_ids=frozenset({"dana"}),
            compartments=frozenset({"hr"}),
            owner_ids=frozenset(),
        )
        assert engine.evaluate_read(ctx("henry", roles=("hr",)), f, snap("henry")).view is View.FULL

    def test_strict_compartments_bind_owner(self) -> None:
        policy = MemoryPolicy(strict_compartments=True)
        f = facts(policy, compartments=frozenset({"legal"}))
        assert engine.evaluate_read(ctx("alice"), f, snap("alice")).decision is Decision.DENY
        assert engine.evaluate_read(ctx("alice", roles=("legal",)), f, snap("alice")).allowed

    def test_subject_relative_rules(self) -> None:
        f = facts(templates.availability_record("apollo"), subject_ids=frozenset({"dana"}), owner_ids=frozenset())
        assert engine.evaluate_read(ctx("dana"), f, snap("dana")).view is View.FULL
        pm = engine.evaluate_read(ctx("paula", roles=("project_manager",)), f, snap("paula", relations=MEMBER))
        assert pm.view is View.REDACTED and pm.decision is Decision.ALLOW_REDACTED
        member = engine.evaluate_read(ctx("bob"), f, snap("bob", relations=MEMBER))
        assert member.view is View.AGGREGATE
        # a project_manager outside the project gets nothing
        other = engine.evaluate_read(ctx("pat", roles=("project_manager",)), f, snap("pat"))
        assert other.decision is Decision.DENY

    def test_manager_of_subject(self) -> None:
        policy = MemoryPolicy(
            rules=(
                AccessRule(relation=RelationRequirement(relation="manager_of", target="subject"), view=View.REDACTED),
            )
        )
        f = facts(policy, subject_ids=frozenset({"mallory"}), owner_ids=frozenset())
        ok = engine.evaluate_read(ctx("carol"), f, snap("carol", relations=(("manager_of", "principal", "mallory"),)))
        assert ok.view is View.REDACTED
        assert not engine.evaluate_read(ctx("carol"), f, snap("carol")).allowed

    def test_allowed_views_cap_and_view_not_allowed(self) -> None:
        f = facts(allowed_views=frozenset({View.REDACTED, View.EXISTENCE_ONLY}))
        d = engine.evaluate_read(ctx(), f, snap(relations=MEMBER))
        assert d.view is View.REDACTED and Reason.VIEW_CAPPED in d.reasons
        policy = MemoryPolicy(rules=(AccessRule(tenant_wide=True, view=View.AGGREGATE),))
        f2 = facts(policy, allowed_views=frozenset({View.FULL}), owner_ids=frozenset())
        assert Reason.VIEW_NOT_ALLOWED in engine.evaluate_read(ctx(), f2, snap()).reasons

    def test_existence_acknowledged_only_to_listed_predicates(self) -> None:
        policy = MemoryPolicy(existence_visible_to=(PrincipalPredicate(any_roles=frozenset({"manager"})),))
        f = facts(policy, owner_ids=frozenset())
        assert engine.evaluate_read(ctx("carol", roles=("manager",)), f, snap("carol")).existence_ack
        assert not engine.evaluate_read(ctx("bob"), f, snap()).existence_ack

    def test_named_principal_rules(self) -> None:
        named = MemoryPolicy(rules=(AccessRule(principal_ids=frozenset({"bob", "carol"}), view=View.REDACTED),))
        f = facts(named, owner_ids=frozenset())
        d = engine.evaluate_read(ctx("bob"), f, snap("bob"))
        assert d.view is View.REDACTED and Reason.NAMED_PRINCIPAL_RULE in d.reasons
        assert not engine.evaluate_read(ctx("mallory"), f, snap("mallory")).allowed
        both = MemoryPolicy(
            rules=(AccessRule(principal_ids=frozenset({"bob"}), any_roles=frozenset({"hr"}), view=View.FULL),)
        )
        assert not engine.evaluate_read(ctx("bob"), facts(both, owner_ids=frozenset()), snap("bob")).allowed
        assert engine.evaluate_read(ctx("bob", roles=("hr",)), facts(both, owner_ids=frozenset()), snap("bob")).allowed
        with pytest.raises(ValueError):
            AccessRule(principal_ids=frozenset({"bob"}), tenant_wide=True, view=View.FULL)

    def test_rule_attributes(self) -> None:
        policy = MemoryPolicy(
            rules=(AccessRule(tenant_wide=True, view=View.FULL, require_attributes=(("site", "sg"),)),)
        )
        f = facts(policy, owner_ids=frozenset())
        c = ctx().model_copy(update={"attributes": (("site", "sg"),)})
        assert engine.evaluate_read(c, f, snap()).allowed
        assert not engine.evaluate_read(ctx(), f, snap()).allowed


class TestHistoryAndLineage:
    def test_historical_owners_only_by_default(self) -> None:
        f = facts(MemoryPolicy(rules=(AccessRule(tenant_wide=True, view=View.FULL),)))
        assert engine.evaluate_read(ctx("alice"), f, snap("alice"), historical=True).allowed
        d = engine.evaluate_read(ctx("bob"), f, snap("bob"), historical=True)
        assert d.decision is Decision.DENY and Reason.HISTORY_NOT_AUTHORIZED in d.reasons

    def test_historical_same_as_current(self) -> None:
        assert engine.evaluate_read(ctx(), facts(), snap(relations=MEMBER), historical=True).allowed

    def test_derived_inherits_minimum_parent_view(self) -> None:
        derived = facts(
            MemoryPolicy(),
            memory_id="mem_d",
            is_derived=True,
            inherits_parent_policy=True,
            parent_ids=("mem_a", "mem_b"),
            owner_ids=frozenset(),
        )
        pa = engine.evaluate_read(ctx(), facts(memory_id="mem_a"), snap(relations=MEMBER))
        pb_facts = facts(
            templates.availability_record("apollo"),
            memory_id="mem_b",
            subject_ids=frozenset({"dana"}),
            owner_ids=frozenset(),
        )
        pb = engine.evaluate_read(ctx(), pb_facts, snap(relations=MEMBER))
        assert pa.view is View.FULL and pb.view is View.AGGREGATE
        d = engine.evaluate_read(ctx(), derived, snap(relations=MEMBER), parent_decisions={"mem_a": pa, "mem_b": pb})
        assert d.view is View.AGGREGATE and Reason.INHERITED_FROM_PARENTS in d.reasons

    def test_derived_denied_if_any_parent_denied_or_unavailable(self) -> None:
        derived = facts(
            MemoryPolicy(), memory_id="mem_d", inherits_parent_policy=True, is_derived=True, parent_ids=("mem_a",)
        )
        # even an owner of the derived memory cannot read it without the parent
        assert engine.evaluate_read(ctx("alice"), derived, snap("alice"), parent_decisions={}).decision is Decision.DENY
        gone = engine.evaluate_read(ctx(), facts(memory_id="mem_a", lifecycle_state=LifecycleState.TOMBSTONED), snap())
        d = engine.evaluate_read(ctx(), derived, snap(), parent_decisions={"mem_a": gone})
        assert d.decision is Decision.UNAVAILABLE and Reason.PARENT_UNAVAILABLE in d.reasons


class TestWrites:
    def test_owner_may_update_delete_grant(self) -> None:
        for action in (WriteAction.UPDATE, WriteAction.DELETE, WriteAction.GRANT):
            assert engine.authorize_write(ctx("alice"), facts(), snap("alice"), action).allowed

    def test_reader_without_authority_is_forbidden_not_hidden(self) -> None:
        d = engine.authorize_write(ctx(), facts(), snap(relations=MEMBER), WriteAction.DELETE)
        assert d.decision is Decision.DENY and Reason.NOT_AUTHORIZED_TO_WRITE in d.reasons

    def test_invisible_memory_is_not_revealed_by_write(self) -> None:
        d = engine.authorize_write(ctx("carol"), facts(), snap("carol"), WriteAction.DELETE)
        assert d.decision is Decision.DENY and Reason.NOT_AUTHORIZED_TO_WRITE not in d.reasons

    def test_delete_authority_and_subject_deletion(self) -> None:
        f = facts(templates.availability_record("apollo"), subject_ids=frozenset({"dana"}), owner_ids=frozenset())
        assert engine.authorize_write(ctx("henry", roles=("hr",)), f, snap("henry"), WriteAction.DELETE).allowed
        assert engine.authorize_write(ctx("dana"), f, snap("dana"), WriteAction.DELETE).allowed
        assert not engine.authorize_write(ctx("dana"), f, snap("dana"), WriteAction.GRANT).allowed

    def test_delete_of_superseded_memory_with_require_version_false(self) -> None:
        old = VersionFacts("ver_1", VersionState.SUPERSEDED, NOW - timedelta(days=2), NOW - timedelta(days=1))
        f = facts(version=old)
        assert not engine.authorize_write(ctx("alice"), f, snap("alice"), WriteAction.DELETE).allowed
        assert engine.authorize_write(ctx("alice"), f, snap("alice"), WriteAction.DELETE, require_version=False).allowed

    def test_derive_requires_full_view(self) -> None:
        f = facts(templates.availability_record("apollo"), subject_ids=frozenset({"dana"}), owner_ids=frozenset())
        d = engine.authorize_write(
            ctx("paula", roles=("project_manager",)), f, snap("paula", relations=MEMBER), WriteAction.DERIVE
        )
        assert not d.allowed

    def test_decisions_carry_version_and_ids(self) -> None:
        d = engine.evaluate_read(ctx(), facts(policy_revision=7), snap(relations=MEMBER))
        assert d.policy_version.endswith("#r7") and d.memory_id == "mem_1" and d.principal_id == "bob"
        assert d.decided_at == NOW
