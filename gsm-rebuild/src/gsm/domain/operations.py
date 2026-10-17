"""Strictly typed candidate memory operations.

Operations are *proposals*. Whether they come from a trusted structured
caller (``Origin.ORACLE``) or from a model (``Origin.MODEL``), they pass
through validation and authorization before anything is committed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gsm.domain.content import Content
from gsm.domain.policy import MemoryPolicy, ObjId, Purpose, RelName, TypeName
from gsm.domain.types import EdgeType, OperationStatus, OpType, SemanticType, Sensitivity, View

Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")]
Attribute = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,127}$")]
Topic = Annotated[str, StringConstraints(min_length=1, max_length=200)]

ALL_VIEWS = frozenset(View)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PolicyTemplateRef(_Strict):
    """Reference to a named template in ``gsm.policy.templates``."""

    name: Literal["private", "hr_restricted", "availability_record", "project", "tenant_public"]
    project_id: ObjId | None = None


class NewFact(_Strict):
    semantic_type: SemanticType
    content_schema: str = "note"
    content: Content
    topic: Topic
    # Semantic identity attribute for single-valued facts (see docs/TEMPORAL.md).
    attribute: Attribute | None = None
    subject_ids: tuple[Ident, ...] = ()
    owner_ids: tuple[Ident, ...] | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    compartments: frozenset[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = frozenset()
    policy: MemoryPolicy | None = None
    policy_template: PolicyTemplateRef | None = None
    allowed_views: frozenset[View] = ALL_VIEWS
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    retain_until: datetime | None = None
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> NewFact:
        if self.policy is not None and self.policy_template is not None:
            raise ValueError("give either policy or policy_template, not both")
        if not self.allowed_views:
            raise ValueError("allowed_views must not be empty")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if self.semantic_type in (SemanticType.SUMMARY, SemanticType.REPORT):
            raise ValueError("summaries and reports are created through derive, with provenance")
        return self


class _Op(_Strict):
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class AddFact(_Op):
    op: Literal[OpType.ADD_FACT] = OpType.ADD_FACT
    fact: NewFact


class UpdateFact(_Op):
    """New immutable version of the same memory (same semantic identity)."""

    op: Literal[OpType.UPDATE_FACT] = OpType.UPDATE_FACT
    memory_id: Ident
    expected_version_id: Ident | None = None
    content: Content
    valid_from: datetime | None = None
    contradiction: bool = False
    # Optional re-classification. Anything that could widen access is a
    # declassification and needs ``declassify=True`` from an authorized oracle.
    sensitivity: Sensitivity | None = None
    compartments: frozenset[str] | None = None
    policy: MemoryPolicy | None = None
    allowed_views: frozenset[View] | None = None
    declassify: bool = False


class SupersedeFact(_Op):
    """A new memory replaces an existing one; the old stays historically auditable."""

    op: Literal[OpType.SUPERSEDE_FACT] = OpType.SUPERSEDE_FACT
    memory_id: Ident
    expected_version_id: Ident | None = None
    replacement: NewFact


class RetireFact(_Op):
    """End the current version's validity without a replacement: the value is outdated.

    Used when the new value comes from someone who may update the memory but shares
    with a different audience, so a supersession would widen access. Retiring only
    narrows (nobody gains access) and keeps the old version historically auditable.
    """

    op: Literal[OpType.RETIRE_FACT] = OpType.RETIRE_FACT
    memory_id: Ident
    expected_version_id: Ident | None = None


class DeleteSelector(_Strict):
    subject_id: Ident
    semantic_type: SemanticType | None = None
    attribute: Attribute | None = None


class DeleteFact(_Op):
    op: Literal[OpType.DELETE_FACT] = OpType.DELETE_FACT
    memory_ids: tuple[Ident, ...] = ()
    selector: DeleteSelector | None = None
    # Required with a selector: the exact number of targets the caller expects.
    expected_count: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _one_scope(self) -> DeleteFact:
        if bool(self.memory_ids) == (self.selector is not None):
            raise ValueError("give exactly one of memory_ids or selector")
        if len(self.memory_ids) > 100:
            raise ValueError("too many targets")
        return self


class GrantAccess(_Op):
    op: Literal[OpType.GRANT_ACCESS] = OpType.GRANT_ACCESS
    memory_id: Ident
    grantee_principal_id: Ident
    view: View
    expires_at: datetime | None = None
    purposes: frozenset[Purpose] = frozenset()


class RestrictAccess(_Op):
    """Withhold one memory from named principals, or lift such a restriction.

    A restriction is an explicit memory-level denial, which outranks every rule and
    grant. Restricting only narrows access and is authorized like an update. Lifting
    widens access again and is authorized like a grant; it lifts only restrictions
    created through this operation, never administrative denials. Owners cannot be
    restricted from their own memory.
    """

    op: Literal[OpType.RESTRICT_ACCESS] = OpType.RESTRICT_ACCESS
    memory_id: Ident
    principal_ids: tuple[Ident, ...] = Field(min_length=1, max_length=50)
    lift: bool = False


class RevokeAccess(_Op):
    op: Literal[OpType.REVOKE_ACCESS] = OpType.REVOKE_ACCESS
    grant_id: Ident


class AddRelationship(_Op):
    op: Literal[OpType.ADD_RELATIONSHIP] = OpType.ADD_RELATIONSHIP
    subject_principal_id: Ident
    relation: RelName
    object_type: TypeName
    object_id: ObjId
    valid_until: datetime | None = None


class RemoveRelationship(_Op):
    op: Literal[OpType.REMOVE_RELATIONSHIP] = OpType.REMOVE_RELATIONSHIP
    relationship_id: Ident


class NoMemoryChange(_Op):
    op: Literal[OpType.NO_MEMORY_CHANGE] = OpType.NO_MEMORY_CHANGE


CandidateMemoryOperation = Annotated[
    AddFact
    | UpdateFact
    | SupersedeFact
    | RetireFact
    | DeleteFact
    | GrantAccess
    | RestrictAccess
    | RevokeAccess
    | AddRelationship
    | RemoveRelationship
    | NoMemoryChange,
    Field(discriminator="op"),
]


class OperationBatch(_Strict):
    operations: tuple[CandidateMemoryOperation, ...] = Field(min_length=1, max_length=100)


class DeriveRequest(_Strict):
    parent_ids: tuple[Ident, ...] = Field(min_length=1, max_length=50)
    semantic_type: Literal[SemanticType.SUMMARY, SemanticType.REPORT, SemanticType.FACT] = SemanticType.SUMMARY
    edge_type: Literal[EdgeType.SUMMARY_OF, EdgeType.REPORT_OF, EdgeType.DERIVED_FROM] = EdgeType.SUMMARY_OF
    content: Content
    topic: Topic
    # Explicit, authorized declassification: the derived memory is governed by
    # this policy alone instead of by its live parents. Only an owner of every
    # parent may declassify.
    declassification: MemoryPolicy | None = None


class StructuredEvent(_Strict):
    event_type: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]
    observed_at: datetime | None = None
    payload: dict[str, object] | None = None
    operations: tuple[CandidateMemoryOperation, ...] = ()


class NaturalLanguageEvent(_Strict):
    event_type: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,63}$")] = "message"
    text: str = Field(min_length=1, max_length=16000)
    observed_at: datetime | None = None


class OperationResult(_Strict):
    index: int
    op: OpType
    status: OperationStatus
    reasons: tuple[str, ...]
    memory_id: str | None = None
    version_id: str | None = None
    grant_id: str | None = None
    relationship_id: str | None = None


class CommitReceipt(_Strict):
    receipt_id: str
    correlation_id: str
    status: Literal["committed", "rejected", "rolled_back", "partial_quarantine", "noop"]
    results: tuple[OperationResult, ...]
    policy_version: str
    event_id: str | None = None


class DeletionRequest(_Strict):
    memory_ids: tuple[Ident, ...] = Field(min_length=1, max_length=100)
    reason_code: Literal["USER_REQUESTED_DELETION", "RETENTION_POLICY", "LEGAL_REQUEST", "CORRECTION"] = (
        "USER_REQUESTED_DELETION"
    )


class DeletionReceipt(_Strict):
    receipt_id: str
    correlation_id: str
    memory_ids: tuple[str, ...]
    tombstone_ids: tuple[str, ...]
    versions_tombstoned: int
    descendants_quarantined: int
    cascade_ids: tuple[str, ...]
    cascade_status: str
    policy_version: str
    created_at: datetime
