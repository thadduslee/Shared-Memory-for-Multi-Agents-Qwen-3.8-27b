"""Content-free audit, and release verification with untrusted generators and verifiers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from gsm.domain.operations import DeletionRequest
from gsm.domain.types import QueryAction
from gsm.errors import NotFoundError
from gsm.providers.base import DraftResponse, GeneratorEvidence, GeneratorPlan, SemanticVerdict
from gsm.providers.fake import StaticSemanticVerifier
from gsm.service import QueryRequest
from tests.helpers import World, add, apollo_decision, availability_fact

AUDIT_TABLES = ["policy_decisions", "release_audits", "commit_receipts", "deletion_tombstones", "deletion_cascades"]


def dump_tables(engine: Engine, tables: list[str]) -> str:
    with engine.connect() as conn:
        return "\n".join(str(row) for t in tables for row in conn.execute(text(f"SELECT * FROM {t}")).all())


def test_audit_records_are_content_free(world: World, owner_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="gsm")
    mid = add(world, "alice", apollo_decision())
    leave = add(world, "dana", availability_fact())
    secret_query = "Why is Dana Reyes out, is it the knee surgery?"
    world.service.query(world.ctx("paula"), QueryRequest(query=secret_query))
    world.service.query(world.ctx("bob"), QueryRequest(query="Apollo launch blue-green"))
    world.service.get_memory(world.ctx("dana"), leave)
    world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    dump = dump_tables(owner_engine, AUDIT_TABLES)
    assert "rel_" in dump and "ALLOW" in dump  # receipts exist...
    for needle in ("blue-green", "October 14", "knee", "surgery", "Dana Reyes", secret_query, "unavailable"):
        assert needle not in dump, needle  # ...without content, queries or answers
        assert needle not in caplog.text
    with owner_engine.connect() as conn:
        fps = [r[0] for r in conn.execute(text("SELECT query_fingerprint FROM release_audits"))]
    assert all(len(fp) == 32 and "dana" not in fp.lower() for fp in fps)


def test_receipts_show_decisions_without_content(world: World) -> None:
    mid = add(world, "alice", apollo_decision())
    receipt = world.service.forget(world.ctx("alice"), DeletionRequest(memory_ids=(mid,)))
    view = world.service.get_receipt(world.ctx("alice"), receipt.receipt_id)
    assert view["status"] == "committed" and view["memory_ids"] == [mid]
    assert view["tombstones"][0]["cascade_status"] == "completed"
    assert any(d["decision"] == "ALLOW" and d["operation"] == "delete" for d in view["decisions"])
    assert "blue-green" not in repr(view)
    with pytest.raises(NotFoundError):
        world.service.get_receipt(world.ctx("bob"), receipt.receipt_id)  # only the actor may inspect it
    status = world.service.get_cascade_status(world.ctx("alice"), receipt.cascade_ids[0])
    assert status.status == "completed" and status.embeddings_deleted > 0


class ScriptedGenerator:
    """A stand-in for an untrusted model."""

    name = "scripted-model"

    def __init__(self, text: str, cite: tuple[str, ...] = (), action: QueryAction | None = None) -> None:
        self.text, self.cite, self.action = text, cite, action
        from gsm.providers.base import ProviderMetrics

        self.metrics = ProviderMetrics()

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        cite = self.cite or tuple(e.memory_id for e in evidence)
        return DraftResponse(
            action=self.action or plan.action, text=self.text, cited_memory_ids=cite, generator=self.name
        )


class ExplodingGenerator(ScriptedGenerator):
    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        raise RuntimeError("model down")


def ask_paula(world: World) -> tuple[QueryAction, str, tuple[str, ...]]:
    r = world.service.query(world.ctx("paula"), QueryRequest(query="Is Dana Reyes unavailable, until when?"))
    return r.action, r.answer, r.reason_codes


class TestReleaseVerification:
    def test_protected_value_in_model_draft_is_blocked(self, world: World) -> None:
        add(world, "dana", availability_fact())
        world.service.providers.generator = ScriptedGenerator("Dana is out for knee surgery recovery until Oct 20.")
        action, answer, reasons = ask_paula(world)
        assert "surgery" not in answer and "knee" not in answer
        assert action is QueryAction.ANSWER_REDACTED  # safe deterministic re-render of the permitted view
        assert "PROTECTED_VALUE_DETECTED" in reasons and "FALLBACK_DETERMINISTIC_RENDER" in reasons

    def test_prohibited_category_in_model_draft_is_blocked(self, world: World) -> None:
        add(world, "dana", availability_fact("recovering from an operation"))
        world.service.providers.generator = ScriptedGenerator("Dana is on medical leave, hospital stay.")
        _, answer, reasons = ask_paula(world)
        assert "medical" not in answer and "PROHIBITED_CATEGORY_DETECTED" in reasons

    def test_unauthorized_memory_citation_is_blocked(self, world: World) -> None:
        add(world, "dana", availability_fact())
        hidden = add(world, "alice", apollo_decision())
        world.service.providers.generator = ScriptedGenerator(f"See {hidden} for details.", cite=(hidden,))
        _, answer, reasons = ask_paula(world)
        assert hidden not in answer and "UNAPPROVED_MEMORY_CITED" in reasons

    def test_draft_inconsistent_with_redacted_plan_is_rewritten_safely(self, world: World) -> None:
        add(world, "dana", availability_fact())
        world.service.providers.generator = ScriptedGenerator("Full record: everything.", action=QueryAction.ANSWER)
        action, answer, reasons = ask_paula(world)
        assert action is QueryAction.ANSWER_REDACTED and "ACTION_MISMATCH" in reasons
        assert "Dana Reyes is unavailable until 2026-10-20" in answer

    def test_semantic_verifier_cannot_override_deterministic_block(self, world: World) -> None:
        add(world, "dana", availability_fact())
        world.service.providers.generator = ScriptedGenerator("Knee surgery recovery, back Oct 20.")
        world.service.providers.semantic_verifier = StaticSemanticVerifier(SemanticVerdict(block=False, confidence=1.0))
        _, answer, reasons = ask_paula(world)
        assert "surgery" not in answer.lower() and "PROTECTED_VALUE_DETECTED" in reasons

    def test_semantic_verifier_may_only_make_outcome_stricter(self, world: World) -> None:
        add(world, "dana", availability_fact())
        world.service.providers.generator = ScriptedGenerator("Dana Reyes is unavailable until 2026-10-20.")
        world.service.providers.semantic_verifier = StaticSemanticVerifier(SemanticVerdict(block=True, confidence=0.9))
        action, answer, reasons = ask_paula(world)
        assert "SEMANTIC_VERIFIER_BLOCK" in reasons and "FALLBACK_DETERMINISTIC_RENDER" in reasons
        world.service.providers.generator = world.service.fallback_generator
        action, answer, reasons = ask_paula(world)
        assert action is QueryAction.REFUSE and "Dana" not in answer

    def test_generator_failure_falls_back_safely(self, world: World) -> None:
        add(world, "dana", availability_fact())
        world.service.providers.generator = ExplodingGenerator("")
        action, answer, _ = ask_paula(world)
        assert action is QueryAction.ANSWER_REDACTED and "surgery" not in answer
        assert world.service.stats["generator_failures"] == 1

    def test_model_output_in_a_no_memory_plan_is_replaced(self, world: World) -> None:
        add(world, "dana", availability_fact())
        world.service.providers.generator = ScriptedGenerator(
            "I can't say, but Dana had knee surgery.", action=QueryAction.NO_MEMORY
        )
        r = world.service.query(world.ctx("carol"), QueryRequest(query="Dana availability"))
        assert r.action is QueryAction.NO_MEMORY and r.answer == "I don't have any information about that."


class TestAnswererReleasePolicy:
    def service_with_policy(self, world: World, app_db: Any, settings: Any) -> Any:
        from gsm.service import GovernedMemoryService

        svc = GovernedMemoryService(
            app_db, settings.model_copy(update={"release_action_policy": "answerer"}), clock=world.clock
        )
        world.service, world.auth.service = svc, svc
        return svc

    def test_answerer_may_decline_with_canonical_text(self, world: World, app_db: Any, settings: Any) -> None:
        svc = self.service_with_policy(world, app_db, settings)
        add(world, "dana", availability_fact())
        svc.providers.generator = ScriptedGenerator("I won't discuss Dana's knee surgery.", action=QueryAction.REFUSE)
        r = svc.query(world.ctx("paula"), QueryRequest(query="Is Dana Reyes unavailable, until when?"))
        assert r.action is QueryAction.REFUSE and r.answer == "I can't share that information."
        assert r.evidence == () and "ANSWERER_DECLINED" in r.reason_codes

    def test_answerer_cannot_answer_without_permitted_evidence(self, world: World, app_db: Any, settings: Any) -> None:
        svc = self.service_with_policy(world, app_db, settings)
        add(world, "dana", availability_fact())
        svc.providers.generator = ScriptedGenerator("Dana has knee surgery.", action=QueryAction.ANSWER)
        r = svc.query(world.ctx("carol"), QueryRequest(query="Is Dana Reyes unavailable, until when?"))
        # NO_MEMORY never exposes why (indistinguishable from an empty memory).
        assert r.action is QueryAction.NO_MEMORY and "surgery" not in r.answer

    def test_answerer_answer_label_is_kept_after_content_checks(self, world: World, app_db: Any, settings: Any) -> None:
        svc = self.service_with_policy(world, app_db, settings)
        add(world, "dana", availability_fact())
        svc.providers.generator = ScriptedGenerator(
            "Dana Reyes is unavailable until 2026-10-20.", action=QueryAction.ANSWER
        )
        r = svc.query(world.ctx("paula"), QueryRequest(query="Is Dana Reyes unavailable, until when?"))
        assert r.action is QueryAction.ANSWER and r.reason_codes == ("VERIFIED",)
