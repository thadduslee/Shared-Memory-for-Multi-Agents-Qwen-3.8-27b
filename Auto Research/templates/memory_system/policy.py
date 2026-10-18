"""Domain-independent deny-first authorization policy evaluator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


INACTIVE_STATES = frozenset({
    "deleted", "expired", "cancelled", "canceled", "superseded", "revoked", "invalid",
})


@dataclass(frozen=True)
class Principal:
    principal_id: str
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Resource:
    resource_id: str
    resource_type: str
    owner_id: str
    scope: Mapping[str, str]
    status: str = "active"
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizationRequest:
    principal: Principal
    action: str
    resource: Resource
    context_scope: Mapping[str, str]
    relationships: tuple[Mapping[str, Any], ...] = ()
    explicit_grants: tuple[Mapping[str, Any], ...] = ()
    explicit_denials: tuple[Mapping[str, Any], ...] = ()
    revocations: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    rule_id: str | None = None


class PolicyEvaluator:
    """Evaluate configured RBAC/ABAC/ReBAC rules without domain vocabulary."""

    def __init__(self, policy: Mapping[str, Any] | None = None) -> None:
        self.policy = dict(policy or {})

    def evaluate(self, request: AuthorizationRequest) -> AuthorizationDecision:
        resource = request.resource
        for boundary in self.policy.get("isolation_boundaries", ()):
            expected = request.context_scope.get(str(boundary))
            actual = resource.scope.get(str(boundary))
            if expected is None or actual is None or str(actual) != str(expected):
                return AuthorizationDecision(False, "isolation_boundary")
        invalid_states = set(self.policy.get("inactive_states") or INACTIVE_STATES)
        if resource.status.lower() in invalid_states:
            return AuthorizationDecision(False, "inactive_resource")

        if resource.resource_id in request.revocations:
            return AuthorizationDecision(False, "explicit_revocation")
        if any(self._grant_matches(item, request) for item in request.explicit_denials):
            return AuthorizationDecision(False, "explicit_denial")

        rules = [item for item in self.policy.get("rules", []) if isinstance(item, Mapping)]
        matching_denies = [item for item in rules
                           if item.get("effect") == "deny" and self._rule_matches(item, request)]
        if matching_denies:
            return AuthorizationDecision(False, "policy_denial",
                                         str(matching_denies[0].get("id") or "deny"))

        if request.principal.principal_id and (
                request.principal.principal_id == resource.owner_id and
                request.action in set(self.policy.get("owner_actions") or {"read", "retrieve"})):
            return AuthorizationDecision(True, "ownership", "ownership")
        for grant in request.explicit_grants:
            if self._grant_matches(grant, request):
                return AuthorizationDecision(True, "explicit_grant",
                                             str(grant.get("id") or "explicit_grant"))
        for rule in rules:
            if rule.get("effect") == "allow" and self._rule_matches(rule, request):
                return AuthorizationDecision(True, "policy_allow",
                                             str(rule.get("id") or "allow"))
        return AuthorizationDecision(False, "default_deny")

    @staticmethod
    def _one_of(actual: Iterable[str], expected: Any) -> bool:
        wanted = {str(value) for value in (expected if isinstance(expected, list) else [expected])}
        return bool(set(actual) & wanted)

    def _grant_matches(self, grant: Mapping[str, Any], request: AuthorizationRequest) -> bool:
        if grant.get("status", "active") != "active":
            return False
        if grant.get("principal_id") not in {None, "", request.principal.principal_id}:
            return False
        if grant.get("action") not in {None, "", request.action}:
            return False
        if grant.get("resource_id") not in {None, "", request.resource.resource_id}:
            return False
        if grant.get("resource_type") not in {None, "", request.resource.resource_type}:
            return False
        scope = grant.get("scope") or {}
        return all(request.resource.scope.get(str(key)) == str(value)
                   for key, value in scope.items())

    def _rule_matches(self, rule: Mapping[str, Any], request: AuthorizationRequest) -> bool:
        if rule.get("actions") and request.action not in set(rule["actions"]):
            return False
        if rule.get("resource_types") and request.resource.resource_type not in set(
                rule["resource_types"]):
            return False
        principal = request.principal
        if rule.get("principal_ids") and principal.principal_id not in set(rule["principal_ids"]):
            return False
        if rule.get("roles") and not self._one_of(principal.roles, rule["roles"]):
            return False
        if rule.get("groups") and not self._one_of(principal.groups, rule["groups"]):
            return False
        if rule.get("capabilities") and not self._one_of(
                principal.capabilities, rule["capabilities"]):
            return False
        for key, value in (rule.get("principal_attributes") or {}).items():
            if principal.attributes.get(key) != value:
                return False
        for key, value in (rule.get("resource_attributes") or {}).items():
            actual = request.resource.attributes.get(key)
            if isinstance(value, list):
                if not set(actual if isinstance(actual, (list, tuple, set)) else [actual]) & set(value):
                    return False
            elif actual != value:
                return False
        for key, value in (rule.get("context_attributes") or {}).items():
            if request.context_scope.get(key) != value:
                return False
        relation = rule.get("relationship")
        if relation and not self._relationship_matches(relation, request):
            return False
        return True

    @staticmethod
    def _relationship_matches(spec: Mapping[str, Any], request: AuthorizationRequest) -> bool:
        predicates = set(spec.get("predicates") or [])
        for relation in request.relationships:
            if relation.get("status", "active") != "active":
                continue
            if predicates and relation.get("predicate") not in predicates:
                continue
            direction = spec.get("direction", "principal_to_owner")
            if direction == "principal_to_owner":
                if (relation.get("subject_id") == request.principal.principal_id and
                        relation.get("object_id") == request.resource.owner_id):
                    return True
            elif (relation.get("object_id") == request.principal.principal_id and
                  relation.get("subject_id") == request.resource.owner_id):
                return True
        return False
