"""Deterministic, in-process policy engine.

Read-decision precedence (first matching stage wins; stages 5-7 are all allow
paths and the most revealing applicable view is taken):

    1. tenant / workspace mismatch, inactive principal     -> DENY
    2. tombstoned or pending cascade invalidation           -> UNAVAILABLE
    3. expired retention, no version valid at the time      -> UNAVAILABLE
    4. explicit denial (memory, compartment or subject)     -> DENY
    4b. mandatory constraints (purpose, auth strength)      -> DENY, or ESCALATE
        when the requester would otherwise be allowed
    5. owner access (policy.owner_view)                     -> allow owner view
    6. active explicit grant                                -> allow grant view
    7. matching rule (role / relationship / attributes,
       with compartment clearance)                          -> allow rule view
       -- derived memories replace 5-7 with the minimum view
          over their live parent decisions
    8. otherwise                                            -> DENY

After an allow: the view is capped by the memory's ``allowed_views`` and, for
historical versions, ``policy.historical_access`` is enforced.

The engine never sees memory content and never consults request text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from gsm import POLICY_ENGINE_VERSION
from gsm.domain.context import PrincipalContext
from gsm.domain.policy import AccessRule, MemoryPolicy, PrincipalPredicate
from gsm.domain.reasons import Reason
from gsm.domain.types import (
    AuthStrength,
    Decision,
    LifecycleState,
    Sensitivity,
    VersionState,
    View,
    cap_view,
)
from gsm.policy.decision import PolicyDecision
from gsm.policy.facts import AuthSnapshot, GrantFact, MemoryFacts


class WriteAction(StrEnum):
    UPDATE = "update"
    DELETE = "delete"
    GRANT = "grant"
    DERIVE = "derive"


@dataclass(frozen=True)
class EngineConfig:
    # Organization-wide clearances: role -> compartments it may read under rules.
    role_compartments: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: {
            "hr": frozenset({"hr", "medical_leave"}),
            "security": frozenset({"credentials", "security"}),
            "legal": frozenset({"legal"}),
            "finance": frozenset({"finance"}),
        }
    )
    # Minimum authentication strength for any access at each sensitivity.
    sensitivity_min_auth: Mapping[Sensitivity, AuthStrength] = field(
        default_factory=lambda: {
            Sensitivity.PUBLIC: AuthStrength.NONE,
            Sensitivity.INTERNAL: AuthStrength.NONE,
            Sensitivity.CONFIDENTIAL: AuthStrength.PASSWORD,
            Sensitivity.RESTRICTED: AuthStrength.MFA,
        }
    )


@dataclass
class _Access:
    view: View
    reasons: list[Reason]


class PolicyEngine:
    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def policy_version(facts: MemoryFacts | None) -> str:
        if facts is None:
            return POLICY_ENGINE_VERSION
        return f"{POLICY_ENGINE_VERSION}#r{facts.policy_revision}"

    def _decision(
        self,
        ctx: PrincipalContext,
        facts: MemoryFacts | None,
        decision: Decision,
        reasons: Iterable[Reason],
        view: View | None = None,
        existence_ack: bool = False,
    ) -> PolicyDecision:
        uniq: list[Reason] = []
        for r in reasons:
            if r not in uniq:
                uniq.append(r)
        return PolicyDecision(
            decision=decision,
            view=view,
            reasons=tuple(uniq),
            policy_version=self.policy_version(facts),
            memory_id=facts.memory_id if facts else None,
            principal_id=ctx.principal_id,
            decided_at=ctx.request_time,
            existence_ack=existence_ack if decision is Decision.DENY else False,
        )

    def cleared_compartments(self, ctx: PrincipalContext, snap: AuthSnapshot) -> frozenset[str]:
        cleared: set[str] = set()
        for role in ctx.roles:
            cleared |= self.config.role_compartments.get(role, frozenset())
        now = ctx.request_time
        for g in snap.compartment_grants:
            if g.compartment and _grant_active(g, now, ctx.purpose):
                cleared.add(g.compartment)
        return frozenset(cleared)

    def predicate_matches(
        self, ctx: PrincipalContext, snap: AuthSnapshot, pred: PrincipalPredicate, facts: MemoryFacts
    ) -> Reason | None:
        """Return the reason code for a match, or None."""
        if pred.tenant_wide:
            return Reason.TENANT_WIDE_RULE
        if pred.principal_ids and ctx.principal_id not in pred.principal_ids:
            return None
        if pred.any_roles and not (pred.any_roles & ctx.roles):
            return None
        rel = pred.relation
        if rel is None:
            return Reason.ROLE_RULE if pred.any_roles else Reason.NAMED_PRINCIPAL_RULE
        now = ctx.request_time
        if rel.target == "object":
            assert rel.object_type is not None and rel.object_id is not None
            ok = snap.has_relation(rel.relation, rel.object_type, rel.object_id, now)
            return Reason.RELATIONSHIP_RULE if ok else None
        if rel.relation == "self":
            return Reason.SUBJECT_SELF_RULE if ctx.principal_id in facts.subject_ids else None
        ok = any(snap.has_relation(rel.relation, "principal", s, now) for s in facts.subject_ids)
        return Reason.RELATIONSHIP_RULE if ok else None

    def _rule_matches(
        self, ctx: PrincipalContext, snap: AuthSnapshot, rule: AccessRule, facts: MemoryFacts
    ) -> tuple[Reason | None, Reason | None]:
        """(match_reason, failure_reason)."""
        base = self.predicate_matches(ctx, snap, rule, facts)
        if base is None:
            return None, None
        if rule.purposes and (ctx.purpose is None or ctx.purpose not in rule.purposes):
            return None, Reason.PURPOSE_NOT_PERMITTED
        if ctx.auth_strength.rank < rule.min_auth_strength.rank:
            return None, Reason.AUTH_STRENGTH_INSUFFICIENT
        for key, value in rule.require_attributes:
            if ctx.attribute(key) != value:
                return None, Reason.NO_APPLICABLE_RULE
        return base, None

    def _explicitly_denied(self, ctx: PrincipalContext, snap: AuthSnapshot, facts: MemoryFacts) -> bool:
        now = ctx.request_time
        for d in snap.denials:
            if d.lifted_at is not None and d.lifted_at <= now:
                continue
            if d.valid_from > now or (d.valid_until is not None and d.valid_until <= now):
                continue
            if (
                (d.resource_type == "memory" and d.resource_id == facts.memory_id)
                or (d.resource_type == "compartment" and d.resource_id in facts.compartments)
                or (d.resource_type == "subject" and d.resource_id in facts.subject_ids)
            ):
                return True
        return False

    def _mandatory_failure(
        self, ctx: PrincipalContext, facts: MemoryFacts, policy: MemoryPolicy
    ) -> tuple[Reason, bool] | None:
        """Returns (reason, escalatable) when a mandatory constraint fails."""
        m = policy.mandatory
        if m.purposes:
            if ctx.purpose is None:
                return Reason.PURPOSE_MISSING, True
            if ctx.purpose not in m.purposes:
                return Reason.PURPOSE_NOT_PERMITTED, False
        required = m.min_auth_strength
        floor = self.config.sensitivity_min_auth.get(facts.sensitivity, AuthStrength.MFA)
        if floor.rank > required.rank:
            required = floor
        if ctx.auth_strength.rank < required.rank:
            return Reason.AUTH_STRENGTH_INSUFFICIENT, True
        return None

    def _existence_ack(
        self, ctx: PrincipalContext, snap: AuthSnapshot, facts: MemoryFacts, policy: MemoryPolicy
    ) -> bool:
        if facts.inherits_parent_policy:
            return False
        return any(self.predicate_matches(ctx, snap, p, facts) for p in policy.existence_visible_to)

    def _own_access(
        self, ctx: PrincipalContext, snap: AuthSnapshot, facts: MemoryFacts, policy: MemoryPolicy
    ) -> tuple[_Access | None, list[Reason]]:
        failures: list[Reason] = []
        best: _Access | None = None
        cleared = self.cleared_compartments(ctx, snap)
        compartments_ok = facts.compartments <= cleared

        def offer(view: View, reason: Reason) -> None:
            nonlocal best
            if best is None or view.rank > best.view.rank:
                best = _Access(view, [reason])
            elif view.rank == best.view.rank:
                best.reasons.append(reason)

        # 5. owner
        if ctx.principal_id in facts.owner_ids and policy.owner_view is not None:
            if policy.strict_compartments and not compartments_ok:
                failures.append(Reason.COMPARTMENT_NOT_CLEARED)
            else:
                offer(policy.owner_view, Reason.OWNER_ACCESS)
        # 6. explicit grants on this memory
        for g in snap.memory_grants.get(facts.memory_id, ()):
            if g.view is None or not _grant_active(g, ctx.request_time, ctx.purpose):
                continue
            if policy.strict_compartments and not compartments_ok:
                failures.append(Reason.COMPARTMENT_NOT_CLEARED)
                continue
            offer(g.view, Reason.EXPLICIT_GRANT)
        # 7. rules
        for rule in policy.rules:
            match, failure = self._rule_matches(ctx, snap, rule, facts)
            if failure is not None:
                failures.append(failure)
            if match is None:
                continue
            if not compartments_ok:
                failures.append(Reason.COMPARTMENT_NOT_CLEARED)
                continue
            offer(rule.view, match)
        return best, failures

    @staticmethod
    def _inherited_access(
        facts: MemoryFacts, parent_decisions: Mapping[str, PolicyDecision] | None
    ) -> tuple[_Access | None, Decision | None, Reason | None]:
        if not facts.parent_ids:
            return None, Decision.DENY, Reason.PARENT_NOT_AUTHORIZED
        parent_decisions = parent_decisions or {}
        view: View | None = View.FULL
        for pid in facts.parent_ids:
            d = parent_decisions.get(pid)
            if d is None:
                return None, Decision.DENY, Reason.PARENT_NOT_AUTHORIZED
            if d.decision is Decision.UNAVAILABLE:
                return None, Decision.UNAVAILABLE, Reason.PARENT_UNAVAILABLE
            if not d.allowed or d.view is None:
                return None, Decision.DENY, Reason.PARENT_NOT_AUTHORIZED
            view = d.view if view is None or d.view.rank < view.rank else view
        assert view is not None
        return _Access(view, [Reason.INHERITED_FROM_PARENTS]), None, None

    # ------------------------------------------------------------------ reads
    def evaluate_read(
        self,
        ctx: PrincipalContext,
        facts: MemoryFacts,
        snap: AuthSnapshot,
        *,
        at: datetime | None = None,
        historical: bool = False,
        parent_decisions: Mapping[str, PolicyDecision] | None = None,
        require_version: bool = True,
    ) -> PolicyDecision:
        """Decide whether ``ctx`` may read ``facts`` and through which view.

        ``at`` is the temporal point of the requested version (defaults to the
        request time). Authorization itself is always evaluated *now*: querying
        the past never restores access that has since been revoked.

        ``require_version=False`` is used only by lifecycle writes (e.g. deleting
        a memory whose versions are all superseded); it skips the version
        validity stage but never the tombstone check.
        """
        at = at or ctx.request_time
        deny = Decision.DENY
        # 1. tenant, workspace, principal
        if facts.tenant_id != ctx.tenant_id:
            return self._decision(ctx, facts, deny, [Reason.TENANT_MISMATCH])
        if facts.workspace_id != ctx.workspace_id:
            return self._decision(ctx, facts, deny, [Reason.WORKSPACE_MISMATCH])
        if snap.tenant_id != ctx.tenant_id or snap.principal_id != ctx.principal_id:
            return self._decision(ctx, facts, deny, [Reason.CONTEXT_UNTRUSTED])
        if not snap.principal_active:
            return self._decision(ctx, facts, deny, [Reason.PRINCIPAL_INACTIVE])
        # 2. lifecycle
        unavailable = Decision.UNAVAILABLE
        if facts.lifecycle_state is LifecycleState.TOMBSTONED:
            return self._decision(ctx, facts, unavailable, [Reason.TOMBSTONED])
        if facts.lifecycle_state is LifecycleState.PENDING_INVALIDATION:
            return self._decision(ctx, facts, unavailable, [Reason.PENDING_INVALIDATION])
        # 3. temporal
        if facts.lifecycle_state is LifecycleState.EXPIRED or (
            facts.retain_until is not None and facts.retain_until <= ctx.request_time
        ):
            return self._decision(ctx, facts, unavailable, [Reason.EXPIRED])
        v = facts.version
        if v is not None and v.state is VersionState.TOMBSTONED:
            return self._decision(ctx, facts, unavailable, [Reason.TOMBSTONED])
        if require_version:
            if v is None:
                return self._decision(ctx, facts, unavailable, [Reason.NO_VERSION_AT_TIME])
            if v.valid_from > at:
                return self._decision(ctx, facts, unavailable, [Reason.NOT_YET_VALID])
            if v.valid_until is not None and v.valid_until <= at:
                return self._decision(ctx, facts, unavailable, [Reason.EXPIRED])
        policy = facts.policy
        if policy is None:
            return self._decision(ctx, facts, deny, [Reason.MALFORMED_POLICY])
        # 4. explicit denial
        if self._explicitly_denied(ctx, snap, facts):
            return self._decision(ctx, facts, deny, [Reason.EXPLICIT_DENIAL])
        # 4b. mandatory constraints
        mandatory = self._mandatory_failure(ctx, facts, policy)
        if mandatory is not None and not mandatory[1]:
            ack = self._existence_ack(ctx, snap, facts, policy)
            return self._decision(ctx, facts, deny, [mandatory[0]], existence_ack=ack)
        # 5-7. allow paths
        if facts.inherits_parent_policy:
            access, terminal, terminal_reason = self._inherited_access(facts, parent_decisions)
            if access is None:
                assert terminal is not None and terminal_reason is not None
                return self._decision(ctx, facts, terminal, [terminal_reason])
            failures: list[Reason] = []
        else:
            access, failures = self._own_access(ctx, snap, facts, policy)
        # 8. default deny
        if access is None:
            ack = self._existence_ack(ctx, snap, facts, policy)
            return self._decision(ctx, facts, deny, [*failures, Reason.NO_APPLICABLE_RULE], existence_ack=ack)
        if mandatory is not None:
            return self._decision(ctx, facts, Decision.ESCALATE, [mandatory[0], Reason.STEP_UP_REQUIRED])
        reasons = list(access.reasons)
        view = cap_view(access.view, facts.allowed_views)
        if view is None:
            return self._decision(ctx, facts, deny, [*reasons, Reason.VIEW_NOT_ALLOWED])
        if view is not access.view:
            reasons.append(Reason.VIEW_CAPPED)
        if historical:
            is_owner = ctx.principal_id in facts.owner_ids
            if policy.historical_access == "none" or (policy.historical_access == "owners_only" and not is_owner):
                return self._decision(ctx, facts, deny, [*reasons, Reason.HISTORY_NOT_AUTHORIZED])
            reasons.append(Reason.HISTORY_AUTHORIZED)
        decision = Decision.ALLOW if view is View.FULL else Decision.ALLOW_REDACTED
        return self._decision(ctx, facts, decision, reasons, view=view)

    # ------------------------------------------------------------------ writes
    def authorize_write(
        self,
        ctx: PrincipalContext,
        facts: MemoryFacts,
        snap: AuthSnapshot,
        action: WriteAction,
        *,
        parent_decisions: Mapping[str, PolicyDecision] | None = None,
        require_version: bool = True,
    ) -> PolicyDecision:
        """Authorize a lifecycle change to an existing memory.

        A requester who cannot see a memory at all receives UNAVAILABLE/DENY
        without existence acknowledgement; callers map both to a uniform
        NOT_FOUND so write paths cannot be used to probe for existence.
        """
        read = self.evaluate_read(ctx, facts, snap, parent_decisions=parent_decisions, require_version=require_version)
        if read.decision in (Decision.UNAVAILABLE,) or (read.decision is Decision.DENY and not read.existence_ack):
            return read
        if read.decision is Decision.ESCALATE:
            return read
        policy = facts.policy
        if policy is None:
            return self._decision(ctx, facts, Decision.DENY, [Reason.MALFORMED_POLICY])
        if action is WriteAction.DERIVE:
            if read.view is View.FULL:
                return self._decision(ctx, facts, Decision.ALLOW, [Reason.WRITE_AUTHORIZED, *read.reasons], View.FULL)
            return self._decision(ctx, facts, Decision.DENY, [Reason.NOT_AUTHORIZED_TO_WRITE])
        if ctx.principal_id in facts.owner_ids and policy.owner_view is not None:
            return self._decision(ctx, facts, Decision.ALLOW, [Reason.WRITE_AUTHORIZED, Reason.OWNER_ACCESS])
        preds: tuple[PrincipalPredicate, ...] = {
            WriteAction.UPDATE: policy.update_authority,
            WriteAction.DELETE: policy.delete_authority,
            WriteAction.GRANT: policy.grant_authority,
        }[action]
        for pred in preds:
            match = self.predicate_matches(ctx, snap, pred, facts)
            if match is not None:
                return self._decision(ctx, facts, Decision.ALLOW, [Reason.WRITE_AUTHORIZED, match])
        if action is WriteAction.DELETE and policy.subject_can_delete and ctx.principal_id in facts.subject_ids:
            return self._decision(ctx, facts, Decision.ALLOW, [Reason.WRITE_AUTHORIZED, Reason.SUBJECT_SELF_RULE])
        return self._decision(ctx, facts, Decision.DENY, [Reason.NOT_AUTHORIZED_TO_WRITE])


def _grant_active(g: GrantFact, now: datetime, purpose: str | None) -> bool:
    if g.revoked_at is not None and g.revoked_at <= now:
        return False
    if g.valid_from > now:
        return False
    if g.expires_at is not None and g.expires_at <= now:
        return False
    return not (g.purposes and (purpose is None or purpose not in g.purposes))
