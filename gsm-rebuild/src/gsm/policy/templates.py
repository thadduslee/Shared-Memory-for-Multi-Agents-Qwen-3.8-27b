"""Named policy templates.

Model-backed interpretation may only *select* among these by name; it never
authors policy JSON. ``RESTRICTIVENESS_ORDER`` lets the trusted layer refuse a
model's choice that is weaker than a deterministic floor.
"""

from __future__ import annotations

from gsm.domain.policy import (
    AccessRule,
    MemoryPolicy,
    PrincipalPredicate,
    RelationRequirement,
)
from gsm.domain.types import View


def private() -> MemoryPolicy:
    """Owners only."""
    return MemoryPolicy()


def project(project_id: str, *, view: View = View.FULL, acknowledge_existence: bool = True) -> MemoryPolicy:
    """Readable by project members; existence acknowledged to members only."""
    member = RelationRequirement(relation="member_of", object_type="project", object_id=project_id)
    return MemoryPolicy(
        rules=(AccessRule(relation=member, view=view),),
        existence_visible_to=(PrincipalPredicate(relation=member),) if acknowledge_existence else (),
        historical_access="same_as_current",
    )


def availability_record(project_id: str) -> MemoryPolicy:
    """Private employee availability.

    * the employee (subject) and HR: full record, including the reason;
    * a project manager on the employee's project: redacted availability only;
    * other project members: aggregate only (how many people are unavailable);
    * everyone else: nothing, and existence is not acknowledged.
    """
    member = RelationRequirement(relation="member_of", object_type="project", object_id=project_id)
    return MemoryPolicy(
        rules=(
            AccessRule(relation=RelationRequirement(relation="self", target="subject"), view=View.FULL),
            AccessRule(any_roles=frozenset({"hr"}), view=View.FULL),
            AccessRule(any_roles=frozenset({"project_manager"}), relation=member, view=View.REDACTED),
            AccessRule(relation=member, view=View.AGGREGATE),
        ),
        delete_authority=(PrincipalPredicate(any_roles=frozenset({"hr"})),),
        subject_can_delete=True,
    )


def hr_restricted() -> MemoryPolicy:
    """HR case material: subject and HR only; nobody else learns it exists."""
    return MemoryPolicy(
        rules=(
            AccessRule(relation=RelationRequirement(relation="self", target="subject"), view=View.FULL),
            AccessRule(any_roles=frozenset({"hr"}), view=View.FULL),
        ),
        delete_authority=(PrincipalPredicate(any_roles=frozenset({"hr"})),),
        subject_can_delete=True,
    )


def tenant_public() -> MemoryPolicy:
    return MemoryPolicy(rules=(AccessRule(tenant_wide=True, view=View.FULL),), historical_access="same_as_current")


# Most restrictive first. Used to enforce a deterministic floor on model choices.
RESTRICTIVENESS_ORDER = ("private", "hr_restricted", "availability_record", "project", "tenant_public")
