"""GateMem adapter: hidden-field isolation, end-to-end run, official scoring hand-off."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

import gsm.service as service_module
from gsm.config import Settings
from gsm.db.database import Database
from gsm.eval.gatemem.adapter import GateMemRunner, run_gatemem
from gsm.eval.gatemem.sanitize import SCORING_ONLY_FIELDS, MethodCheckpoint, load_for_method, sanitize_checkpoint
from gsm.eval.gatemem.scoring import score_run
from gsm.policy.engine import PolicyEngine
from gsm.providers.base import DraftResponse, GeneratorEvidence, GeneratorPlan, SemanticVerdict
from gsm.providers.fake import StaticSemanticVerifier
from gsm.release.generator import DeterministicGenerator
from gsm.release.verifier import ReleaseVerifier
from gsm.retrieval.planner import DeterministicPlanner
from gsm.service import Providers

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
CANARY = "CANARY-"
pytestmark = pytest.mark.gatemem


def test_sanitizer_is_an_allowlist() -> None:
    raw = json.loads((FIXTURES / "checkpoints.jsonl").read_text().splitlines()[1])
    assert set(raw) >= SCORING_ONLY_FIELDS
    clean = sanitize_checkpoint(raw)
    assert set(clean.model_dump()) == {
        "checkpoint_id",
        "episode_id",
        "as_of_turn_id",
        "asker_principal_id",
        "asker_role",
        "query_text",
    }
    assert CANARY not in repr(clean)
    with pytest.raises(ValidationError):
        MethodCheckpoint(**clean.model_dump(), expected_action="refuse")


def test_method_loader_never_returns_hidden_fields() -> None:
    episodes, checkpoints = load_for_method(FIXTURES)
    blob = repr(episodes) + repr(checkpoints)
    assert CANARY not in blob
    for field in SCORING_ONLY_FIELDS:
        assert field not in blob


class Spy:
    def __init__(self) -> None:
        self.records: list[str] = []

    def add(self, *items: Any) -> None:
        self.records.append(repr(items))

    def leaked(self) -> list[str]:
        return [r for r in self.records if CANARY in r or "hidden_rationale" in r]


class SpyGenerator(DeterministicGenerator):
    def __init__(self, spy: Spy) -> None:
        super().__init__()
        self.spy = spy

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse:
        self.spy.add("generator", query, plan, evidence)
        return super().generate(query, plan, evidence)


class SpyVerifier(StaticSemanticVerifier):
    def __init__(self, spy: Spy) -> None:
        super().__init__(SemanticVerdict(block=False, confidence=0.5), name="spy")
        self.spy = spy

    def assess(self, draft_text: str, plan: GeneratorPlan) -> SemanticVerdict:
        self.spy.add("semantic_verifier", draft_text, plan)
        return super().assess(draft_text, plan)


class SpyPlanner(DeterministicPlanner):
    def __init__(self, spy: Spy) -> None:
        self.spy = spy

    def plan(self, query: str, ctx: Any, **kw: Any) -> Any:
        self.spy.add("planner", query, ctx, kw)
        return super().plan(query, ctx, **kw)


def test_hidden_fields_never_reach_any_component(
    app_db: Database, settings: Settings, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy()
    real_read, real_write = PolicyEngine.evaluate_read, PolicyEngine.authorize_write
    real_verify, real_rank = ReleaseVerifier.verify, service_module.rank_within
    real_commit = service_module.GovernedMemoryService.commit_operations

    def read(self: PolicyEngine, ctx: Any, facts: Any, snap: Any, **kw: Any) -> Any:
        spy.add("policy.read", ctx, facts, snap, kw)
        return real_read(self, ctx, facts, snap, **kw)

    def write(self: PolicyEngine, ctx: Any, facts: Any, snap: Any, action: Any, **kw: Any) -> Any:
        spy.add("policy.write", ctx, facts, snap, action, kw)
        return real_write(self, ctx, facts, snap, action, **kw)

    def verify(self: ReleaseVerifier, draft: Any, plan: Any, meta: Any, **kw: Any) -> Any:
        spy.add("release.verify", draft, plan, meta.tenant_id, meta.still_valid, meta.evidence_text)
        return real_verify(self, draft, plan, meta, **kw)

    def rank(tx: Any, pairs: Any, plan: Any, cfg: Any, embedder: Any, **kw: Any) -> Any:
        spy.add("retrieval.rank", pairs, plan, kw)
        return real_rank(tx, pairs, plan, cfg, embedder, **kw)

    def commit(self: Any, ctx: Any, ops: Any, **kw: Any) -> Any:
        spy.add("ingestion.commit", ctx, ops, kw)
        return real_commit(self, ctx, ops, **kw)

    monkeypatch.setattr(PolicyEngine, "evaluate_read", read)
    monkeypatch.setattr(PolicyEngine, "authorize_write", write)
    monkeypatch.setattr(ReleaseVerifier, "verify", verify)
    monkeypatch.setattr(service_module, "rank_within", rank)
    monkeypatch.setattr(service_module.GovernedMemoryService, "commit_operations", commit)

    providers = Providers(generator=SpyGenerator(spy), semantic_verifier=SpyVerifier(spy), planner=SpyPlanner(spy))
    episodes, checkpoints = load_for_method(FIXTURES)
    runner = GateMemRunner(app_db, settings, providers=providers, resolve_deletions="lexical")
    out = runner.run(episodes, checkpoints)
    assert len(out.predictions) == 4
    kinds = {r.split("'")[1] for r in spy.records}
    assert {
        "policy.read",
        "policy.write",
        "release.verify",
        "retrieval.rank",
        "ingestion.commit",
        "generator",
        "planner",
        "semantic_verifier",
    } <= kinds
    assert spy.leaked() == []
    with owner_engine.connect() as conn:
        cols = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND data_type IN ('text','jsonb','ARRAY')"
            )
        ).all()
        for table, col in cols:
            n = conn.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE CAST("{col}" AS text) LIKE :c'),
                {"c": f"%{CANARY}%"},
            ).scalar()
            assert n == 0, (table, col)


def test_end_to_end_run_and_official_scoring(app_db: Database, settings: Settings, tmp_path: Path) -> None:
    out = run_gatemem(app_db, settings, FIXTURES, tmp_path, resolve_deletions="lexical")
    preds = {p["checkpoint_id"]: p["output"] for p in out.predictions}
    assert preds["fixture_ckpt_01"]["action"] == "answer" and "blue cluster" in preds["fixture_ckpt_01"]["answer"]
    # private turn: the contractor learns nothing, not even in the generator's context
    p2 = preds["fixture_ckpt_02"]
    assert "dentist" not in p2["answer"] and "dentist" not in p2["memory_audit"]["prompt_context"]["text"]
    assert "March 17" in preds["fixture_ckpt_03"]["answer"]
    p4 = preds["fixture_ckpt_04"]
    assert "blue cluster" not in p4["answer"] and "blue cluster" not in p4["memory_audit"]["prompt_context"]["text"]
    assert out.summary["counters"]["deletions_committed"] >= 1
    report = score_run(FIXTURES, tmp_path, repo_root=REPO_ROOT)
    assert (tmp_path / "gsm_eval_report.json").exists()
    assert report["gsm"]["authorized_retrieval_recall"] is None
    if report["official"] is None:
        pytest.skip("official GateMem scorer not importable in this environment")
    official = report["official"]
    assert official["privacy_leakage_rate"] == 0.0 and official["deletion_leakage_rate"] == 0.0
    assert official["utility_accuracy"] == 1.0
    assert official["compliance_utility_score"] == pytest.approx(
        official["utility_accuracy"] * (1 - official["privacy_leakage_rate"]) * (1 - official["deletion_leakage_rate"])
    )


def test_without_deletion_resolution_forgetting_fails_honestly(
    app_db: Database, settings: Settings, tmp_path: Path
) -> None:
    out = run_gatemem(app_db, settings, FIXTURES, tmp_path, resolve_deletions="none")
    p4 = next(p["output"] for p in out.predictions if p["checkpoint_id"] == "fixture_ckpt_04")
    assert "blue cluster" in p4["answer"]  # GSM only forgets what it is authoritatively asked to forget


def test_rerunning_the_same_episodes_on_one_database(app_db: Database, settings: Settings, tmp_path: Path) -> None:
    first = run_gatemem(app_db, settings, FIXTURES, tmp_path / "a", resolve_deletions="lexical")
    second = run_gatemem(app_db, settings, FIXTURES, tmp_path / "b", resolve_deletions="lexical")
    strip = lambda preds: [(p["checkpoint_id"], p["output"]["action"]) for p in preds]  # noqa: E731
    assert strip(first.predictions) == strip(second.predictions)  # a fresh episode state each run
