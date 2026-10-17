"""Metadata-only inputs to the policy engine.

Nothing here carries memory content: the engine decides from identifiers,
labels and timestamps alone, so authorization can run before any content is
read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from gsm.domain.policy import MemoryPolicy
from gsm.domain.types import LifecycleState, Sensitivity, VersionState, View


@dataclass(frozen=True, slots=True)
class VersionFacts:
    version_id: str
    state: VersionState
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryFacts:
    memory_id: str
    tenant_id: str
    workspace_id: str
    lifecycle_state: LifecycleState
    retain_until: datetime | None
    owner_ids: frozenset[str]
    subject_ids: frozenset[str]
    sensitivity: Sensitivity
    compartments: frozenset[str]
    policy: MemoryPolicy | None  # None = stored policy failed to parse
    policy_revision: int
    allowed_views: frozenset[View]
    is_derived: bool = False
    inherits_parent_policy: bool = False
    parent_ids: tuple[str, ...] = ()
    version: VersionFacts | None = None


@dataclass(frozen=True, slots=True)
class RelationFact:
    relation: str
    object_type: str
    object_id: str
    valid_from: datetime
    valid_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class GrantFact:
    grant_id: str
    memory_id: str | None
    compartment: str | None
    view: View | None
    purposes: frozenset[str]
    valid_from: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    granted_by: str


@dataclass(frozen=True, slots=True)
class DenialFact:
    denial_id: str
    resource_type: str  # memory | compartment | subject
    resource_id: str
    valid_from: datetime
    valid_until: datetime | None
    lifted_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthSnapshot:
    """Everything the engine needs about the requester, loaded once per request."""

    tenant_id: str
    principal_id: str
    principal_active: bool
    taken_at: datetime
    relations: tuple[RelationFact, ...] = ()
    memory_grants: Mapping[str, tuple[GrantFact, ...]] = field(default_factory=dict)
    compartment_grants: tuple[GrantFact, ...] = ()
    denials: tuple[DenialFact, ...] = ()

    def active_relations(self, at: datetime) -> list[RelationFact]:
        return [r for r in self.relations if r.valid_from <= at and (r.valid_until is None or r.valid_until > at)]

    def has_relation(self, relation: str, object_type: str, object_id: str, at: datetime) -> bool:
        return any(
            r.relation == relation and r.object_type == object_type and r.object_id == object_id
            for r in self.active_relations(at)
        )
