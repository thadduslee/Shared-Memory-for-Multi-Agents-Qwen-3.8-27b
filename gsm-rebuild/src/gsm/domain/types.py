"""Closed vocabularies shared by every layer.

Truth, disclosure and retention are deliberately separate dimensions:

* truth state (``TruthState``): is this version the current account of the fact?
* disclosure (``View`` + policy decisions): who may receive which projection?
* retention (``LifecycleState`` / ``VersionState``): may the content exist at all?
"""

from __future__ import annotations

from enum import StrEnum


class SemanticType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    TASK = "task"
    PERMISSION = "permission"
    SUMMARY = "summary"
    REPORT = "report"
    AVAILABILITY = "availability"
    NOTE = "note"
    CREDENTIAL = "credential"


class VersionState(StrEnum):
    """Row-level lifecycle of an immutable version (as required by the spec)."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    TOMBSTONED = "tombstoned"


class TruthState(StrEnum):
    CURRENT = "current"
    CONTRADICTED = "contradicted"
    STALE = "stale"
    SUPERSEDED = "superseded"


class LifecycleState(StrEnum):
    """Retention state of a memory atom. Only ``active`` atoms are retrievable."""

    ACTIVE = "active"
    EXPIRED = "expired"
    PENDING_INVALIDATION = "pending_invalidation"
    TOMBSTONED = "tombstoned"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        return _SENSITIVITY_RANK[self]


_SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.CONFIDENTIAL: 2,
    Sensitivity.RESTRICTED: 3,
}


class View(StrEnum):
    """Disclosure projections, from most to least revealing."""

    FULL = "full"
    REDACTED = "redacted"
    AGGREGATE = "aggregate"
    EXISTENCE_ONLY = "existence_only"

    @property
    def rank(self) -> int:
        return _VIEW_RANK[self]


_VIEW_RANK = {View.FULL: 4, View.REDACTED: 3, View.AGGREGATE: 2, View.EXISTENCE_ONLY: 1}


def min_view(a: View | None, b: View | None) -> View | None:
    if a is None or b is None:
        return None
    return a if a.rank <= b.rank else b


def cap_view(requested: View, allowed: frozenset[View]) -> View | None:
    """Largest allowed view that is not more revealing than ``requested``."""
    candidates = [v for v in allowed if v.rank <= requested.rank]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.rank)


class AuthStrength(StrEnum):
    NONE = "none"
    PASSWORD = "password"  # noqa: S105 - an authentication level, not a secret
    MFA = "mfa"
    HARDWARE = "hardware"

    @property
    def rank(self) -> int:
        return _AUTH_RANK[self]


_AUTH_RANK = {AuthStrength.NONE: 0, AuthStrength.PASSWORD: 1, AuthStrength.MFA: 2, AuthStrength.HARDWARE: 3}


class Decision(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_REDACTED = "ALLOW_REDACTED"
    DENY = "DENY"
    UNAVAILABLE = "UNAVAILABLE"
    ESCALATE = "ESCALATE"


class QueryAction(StrEnum):
    ANSWER = "ANSWER"
    ANSWER_REDACTED = "ANSWER_REDACTED"
    REFUSE = "REFUSE"
    NO_MEMORY = "NO_MEMORY"
    ESCALATE = "ESCALATE"


class OpType(StrEnum):
    ADD_FACT = "ADD_FACT"
    UPDATE_FACT = "UPDATE_FACT"
    SUPERSEDE_FACT = "SUPERSEDE_FACT"
    RETIRE_FACT = "RETIRE_FACT"
    RESTRICT_ACCESS = "RESTRICT_ACCESS"
    DELETE_FACT = "DELETE_FACT"
    GRANT_ACCESS = "GRANT_ACCESS"
    REVOKE_ACCESS = "REVOKE_ACCESS"
    ADD_RELATIONSHIP = "ADD_RELATIONSHIP"
    REMOVE_RELATIONSHIP = "REMOVE_RELATIONSHIP"
    NO_MEMORY_CHANGE = "NO_MEMORY_CHANGE"


HIGH_IMPACT_OPS = frozenset(
    {
        OpType.DELETE_FACT,
        OpType.RETIRE_FACT,
        OpType.RESTRICT_ACCESS,
        OpType.GRANT_ACCESS,
        OpType.REVOKE_ACCESS,
        OpType.ADD_RELATIONSHIP,
        OpType.REMOVE_RELATIONSHIP,
    }
)


class Origin(StrEnum):
    """Who proposed an operation. ``model`` proposals never commit high-impact changes."""

    ORACLE = "oracle"
    MODEL = "model"
    DERIVED = "derived"


class EdgeType(StrEnum):
    """Provenance edges always point from the earlier/parent node to the later/child memory."""

    SOURCE_EVENT = "source_event"
    DERIVED_FROM = "derived_from"
    SUMMARY_OF = "summary_of"
    REPORT_OF = "report_of"
    SUPERSEDES = "supersedes"  # src = superseded memory, dst = memory that replaced it
    CONTRADICTS = "contradicts"


# Edges followed by the deletion cascade. Supersession and contradiction are
# not derivation: deleting an old decision does not delete its replacement.
DERIVATION_EDGES = frozenset({EdgeType.DERIVED_FROM, EdgeType.SUMMARY_OF, EdgeType.REPORT_OF})


class OperationStatus(StrEnum):
    COMMITTED = "committed"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    ROLLED_BACK = "rolled_back"
    NOOP = "noop"
