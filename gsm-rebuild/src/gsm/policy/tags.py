"""Index-backed access tags for SQL candidate pre-filtering.

At write time each memory gets ``access_tags``: for every allow rule, the most
selective condition it requires, encoded as a string. At read time the
requester's tags are derived from trusted context (roles) and the live
relationship snapshot. A memory can only be allowed by a rule if the
requester holds that rule's tag, so ``access_tags && requester_tags`` (plus
owner and grant checks) is a *superset* of what the engine will allow. The
engine then decides exactly, on metadata only, before any content is read.
"""

from __future__ import annotations

from collections.abc import Iterable

from gsm.domain.context import PrincipalContext
from gsm.domain.policy import MemoryPolicy, PrincipalPredicate
from gsm.policy.facts import AuthSnapshot

ANY_AUTHENTICATED = "any"


def _predicate_tags(pred: PrincipalPredicate, subject_ids: Iterable[str]) -> set[str]:
    if pred.tenant_wide:
        return {ANY_AUTHENTICATED}
    if pred.principal_ids:
        # Named principals are the most selective condition a rule can carry.
        return {f"principal:{p}" for p in pred.principal_ids}
    rel = pred.relation
    if rel is not None:
        if rel.target == "object":
            return {f"rel:{rel.relation}:{rel.object_type}:{rel.object_id}"}
        if rel.relation == "self":
            return {f"principal:{s}" for s in subject_ids}
        return {f"rel:{rel.relation}:principal:{s}" for s in subject_ids}
    return {f"role:{r}" for r in pred.any_roles}


def access_tags(policy: MemoryPolicy, subject_ids: Iterable[str]) -> list[str]:
    subjects = list(subject_ids)
    tags: set[str] = set()
    for rule in policy.rules:
        tags |= _predicate_tags(rule, subjects)
    return sorted(tags)


def existence_tags(policy: MemoryPolicy, subject_ids: Iterable[str]) -> list[str]:
    subjects = list(subject_ids)
    tags: set[str] = set()
    for pred in policy.existence_visible_to:
        tags |= _predicate_tags(pred, subjects)
    return sorted(tags)


def requester_tags(ctx: PrincipalContext, snap: AuthSnapshot) -> list[str]:
    tags = {ANY_AUTHENTICATED, f"principal:{ctx.principal_id}"}
    tags |= {f"role:{r}" for r in ctx.roles}
    for rel in snap.active_relations(ctx.request_time):
        tags.add(f"rel:{rel.relation}:{rel.object_type}:{rel.object_id}")
    return sorted(tags)
