"""Operation schema strictness, log redaction, pre-filter tag superset, fake embeddings."""

from __future__ import annotations

import itertools
import json
import logging
import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from gsm.audit.logging import RedactingJsonFormatter
from gsm.domain.context import PrincipalContext
from gsm.domain.operations import CandidateMemoryOperation, DeleteFact
from gsm.domain.policy import AccessRule, MemoryPolicy, RelationRequirement
from gsm.domain.types import AuthStrength, LifecycleState, Sensitivity, VersionState, View
from gsm.policy.engine import PolicyEngine
from gsm.policy.facts import AuthSnapshot, MemoryFacts, RelationFact, VersionFacts
from gsm.policy.tags import access_tags, requester_tags
from gsm.providers.fake import HashingEmbedder

OPS: TypeAdapter[CandidateMemoryOperation] = TypeAdapter(CandidateMemoryOperation)


class TestOperationSchema:
    def test_discriminated_union(self) -> None:
        op = OPS.validate_python(
            {"op": "GRANT_ACCESS", "memory_id": "mem_x", "grantee_principal_id": "bob", "view": "redacted"}
        )
        assert op.op == "GRANT_ACCESS"

    def test_unknown_op_and_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OPS.validate_python({"op": "DROP_TABLE"})
        with pytest.raises(ValidationError):
            OPS.validate_python({"op": "NO_MEMORY_CHANGE", "as_role": "admin"})

    def test_delete_requires_exactly_one_scope(self) -> None:
        with pytest.raises(ValidationError):
            DeleteFact()
        with pytest.raises(ValidationError):
            DeleteFact(memory_ids=("mem_a",), selector={"subject_id": "dana"})  # type: ignore[arg-type]

    def test_summaries_must_go_through_derive(self) -> None:
        with pytest.raises(ValidationError):
            OPS.validate_python(
                {"op": "ADD_FACT", "fact": {"semantic_type": "summary", "content": {"text": "x"}, "topic": "t"}}
            )

    def test_policy_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            MemoryPolicy.model_validate({"rules": [{"view": "full", "any_roles": ["x"], "allow_everyone": True}]})
        with pytest.raises(ValidationError):
            AccessRule(view=View.FULL)  # a rule must name who it applies to


class TestLogging:
    def test_sensitive_keys_are_redacted(self) -> None:
        rec = logging.makeLogRecord(
            {
                "msg": "query_done",
                "query": "Dana's diagnosis?",
                "text": "secret",
                "memory_id": "mem_1",
                "api_key": "sk-123",
            }
        )
        out = json.loads(RedactingJsonFormatter().format(rec))
        assert out["query"] == "[redacted]" and out["text"] == "[redacted]" and out["api_key"] == "[redacted]"
        assert out["memory_id"] == "mem_1"

    def test_exception_messages_are_not_logged(self) -> None:
        try:
            raise ValueError("Dana has cancer")
        except ValueError:
            rec = logging.LogRecord(
                "gsm", logging.ERROR, __file__, 1, "failed", None, exc_info=__import__("sys").exc_info()
            )
        out = RedactingJsonFormatter().format(rec)
        assert "cancer" not in out and "ValueError" in out


NOW = datetime(2026, 9, 1, tzinfo=UTC)


def test_access_tags_are_a_superset_of_rule_allows() -> None:
    """If the engine allows through a rule, the SQL pre-filter tags must intersect."""
    engine = PolicyEngine()
    rels = [("member_of", "project", "apollo"), ("manager_of", "principal", "dana"), ("member_of", "project", "zeus")]
    rules = [
        AccessRule(any_roles=frozenset({"hr"}), view=View.FULL),
        AccessRule(
            relation=RelationRequirement(relation="member_of", object_type="project", object_id="apollo"),
            view=View.FULL,
        ),
        AccessRule(relation=RelationRequirement(relation="manager_of", target="subject"), view=View.REDACTED),
        AccessRule(relation=RelationRequirement(relation="self", target="subject"), view=View.FULL),
        AccessRule(
            any_roles=frozenset({"pm"}),
            relation=RelationRequirement(relation="member_of", object_type="project", object_id="apollo"),
            view=View.REDACTED,
        ),
        AccessRule(tenant_wide=True, view=View.AGGREGATE),
        AccessRule(principal_ids=frozenset({"bob"}), view=View.FULL),
        AccessRule(principal_ids=frozenset({"bob", "dana"}), any_roles=frozenset({"hr"}), view=View.FULL),
        AccessRule(principal_ids=frozenset({"carol"}), view=View.FULL),
    ]
    checked = 0
    for rule, roles, rel_subset, subjects in itertools.product(
        rules, [(), ("hr",), ("pm",)], [(), rels[:1], rels[1:2], rels], [("dana",), ("project:apollo",), ("bob",)]
    ):
        policy = MemoryPolicy(rules=(rule,), owner_view=None)
        facts = MemoryFacts(
            memory_id="m",
            tenant_id="t",
            workspace_id="w",
            lifecycle_state=LifecycleState.ACTIVE,
            retain_until=None,
            owner_ids=frozenset(),
            subject_ids=frozenset(subjects),
            sensitivity=Sensitivity.INTERNAL,
            compartments=frozenset(),
            policy=policy,
            policy_revision=1,
            allowed_views=frozenset(View),
            version=VersionFacts("v", VersionState.ACTIVE, NOW - timedelta(days=1), None),
        )
        ctx = PrincipalContext(
            tenant_id="t",
            workspace_id="w",
            principal_id="bob",
            roles=frozenset(roles),
            auth_time=NOW,
            auth_strength=AuthStrength.PASSWORD,
            request_time=NOW,
            correlation_id="corr_12345678",
            issued_by="test",
        )
        snap = AuthSnapshot("t", "bob", True, NOW, tuple(RelationFact(*r, NOW - timedelta(days=1)) for r in rel_subset))
        if engine.evaluate_read(ctx, facts, snap).allowed:
            checked += 1
            assert set(access_tags(policy, subjects)) & set(requester_tags(ctx, snap))
    assert checked > 10


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    e = HashingEmbedder(64)
    a, b = e.embed(["Apollo launch decision"])[0], HashingEmbedder(64).embed(["Apollo launch decision"])[0]
    assert a == b and len(a) == 64
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    near = e.embed(["Apollo launch plan"])[0]
    far = e.embed(["quarterly tax filing"])[0]
    assert sum(x * y for x, y in zip(a, near, strict=True)) > sum(x * y for x, y in zip(a, far, strict=True))
