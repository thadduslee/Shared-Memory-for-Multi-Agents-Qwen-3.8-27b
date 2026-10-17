"""Declarative per-memory policy.

A policy is data, validated with ``extra="forbid"``: an unknown key or a
malformed rule is rejected at write time, and a stored policy that fails to
parse is treated as ``MALFORMED_POLICY`` (deny) at read time.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from gsm.domain.types import AuthStrength, Sensitivity, View

RelName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
TypeName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ObjId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")]
RoleName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Purpose = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,64}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RelationRequirement(_Frozen):
    """A ReBAC condition.

    ``target="object"``: requester must hold ``relation`` to the fixed object
    (e.g. ``member_of project:apollo``).
    ``target="subject"``: requester must hold ``relation`` to one of the memory's
    subjects (e.g. ``manager_of`` the employee the record is about). The special
    relation ``self`` means the requester *is* one of the subjects.
    """

    relation: RelName
    target: Literal["object", "subject"] = "object"
    object_type: TypeName | None = None
    object_id: ObjId | None = None

    @model_validator(mode="after")
    def _shape(self) -> RelationRequirement:
        if self.target == "object":
            if self.object_type is None or self.object_id is None:
                raise ValueError("object relations need object_type and object_id")
            if self.relation == "self":
                raise ValueError("'self' is only valid with target='subject'")
        elif self.object_type is not None or self.object_id is not None:
            raise ValueError("subject relations must not name an object")
        return self


class PrincipalPredicate(_Frozen):
    """Who a clause applies to. Named principals, roles and relation are
    conjunctive when more than one is given."""

    principal_ids: frozenset[ObjId] = frozenset()
    any_roles: frozenset[RoleName] = frozenset()
    relation: RelationRequirement | None = None
    tenant_wide: bool = False

    @model_validator(mode="after")
    def _non_empty(self) -> PrincipalPredicate:
        if self.tenant_wide and (self.any_roles or self.relation or self.principal_ids):
            raise ValueError("tenant_wide predicates cannot carry other conditions")
        if not self.tenant_wide and not self.any_roles and self.relation is None and not self.principal_ids:
            raise ValueError("a predicate must name principals, roles, a relation, or be tenant_wide")
        return self


class AccessRule(PrincipalPredicate):
    view: View
    purposes: frozenset[Purpose] = frozenset()
    min_auth_strength: AuthStrength = AuthStrength.NONE
    require_attributes: tuple[tuple[str, str], ...] = ()


class MandatoryConstraints(_Frozen):
    """Constraints that bind every allow path, including owners and grantees."""

    purposes: frozenset[Purpose] = frozenset()
    min_auth_strength: AuthStrength = AuthStrength.NONE


class MemoryPolicy(_Frozen):
    schema_version: Literal[1] = 1
    rules: tuple[AccessRule, ...] = ()
    # View owners receive; None removes owner privileges entirely (e.g. records
    # that must be governed only by organizational rules).
    owner_view: View | None = View.FULL
    mandatory: MandatoryConstraints = MandatoryConstraints()
    # Principals who may be told "this exists but you cannot see it" (REFUSE)
    # instead of the indistinguishable NO_MEMORY.
    existence_visible_to: tuple[PrincipalPredicate, ...] = ()
    historical_access: Literal["owners_only", "same_as_current", "none"] = "owners_only"
    update_authority: tuple[PrincipalPredicate, ...] = ()
    delete_authority: tuple[PrincipalPredicate, ...] = ()
    grant_authority: tuple[PrincipalPredicate, ...] = ()
    subject_can_delete: bool = False
    # When true, compartments also bind owners and memory-level grantees.
    strict_compartments: bool = False


def parse_policy(raw: object) -> MemoryPolicy | None:
    """Parse a stored policy. ``None`` means malformed and must be treated as deny."""
    try:
        return MemoryPolicy.model_validate(raw)
    except Exception:
        return None


def is_at_least_as_restrictive(
    new: MemoryPolicy,
    old: MemoryPolicy,
    *,
    new_sensitivity: Sensitivity,
    old_sensitivity: Sensitivity,
    new_compartments: frozenset[str],
    old_compartments: frozenset[str],
    new_views: frozenset[View],
    old_views: frozenset[View],
) -> bool:
    """Conservative check: True only when the change provably cannot widen access.

    Anything this function cannot prove is treated as a broadening and needs an
    explicit, authorized declassification.
    """
    if new_sensitivity.rank < old_sensitivity.rank:
        return False
    if not old_compartments <= new_compartments:
        return False
    if not new_views <= old_views:
        return False
    if not set(new.rules) <= set(old.rules):
        return False
    if not set(new.existence_visible_to) <= set(old.existence_visible_to):
        return False
    if new.owner_view is not None and (old.owner_view is None or new.owner_view.rank > old.owner_view.rank):
        return False
    if old.mandatory.purposes and not (new.mandatory.purposes and new.mandatory.purposes <= old.mandatory.purposes):
        return False
    if new.mandatory.min_auth_strength.rank < old.mandatory.min_auth_strength.rank:
        return False
    order = {"none": 0, "owners_only": 1, "same_as_current": 2}
    if order[new.historical_access] > order[old.historical_access]:
        return False
    for attr in ("update_authority", "delete_authority", "grant_authority"):
        if not set(getattr(new, attr)) <= set(getattr(old, attr)):
            return False
    if new.subject_can_delete and not old.subject_can_delete:
        return False
    return not (old.strict_compartments and not new.strict_compartments)
