"""Blocking online repair, feedback, replay ordering, and durable recovery."""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

import config
from gatemem_adapter import (
    HIDDEN_ANNOTATION_FIELDS,
    MedicalDataset,
    assert_no_hidden_fields,
    load_medical_dataset,
    visible_checkpoint,
)
from nodes.developer import _stop_reason, _turn_budget_block
from online.backend import Backend, DeveloperApproachUnworkable, InfrastructureFailure
from online.metrics import cumulative, promotion_regressions
from online.runner import OnlineOptions, OnlineRunner, RepairBlocked
from online.schedule import build_schedule, compatibility, structural_summary
from online.validation import ValidationFailure, migrate_blob
from templates.memory_system.store import MemoryStore


@pytest.fixture
def dataset():
    episode = {
        "episode_id": "episode",
        "domain": "generic",
        "entities": {
            "principals": [{"principal_id": "owner", "role": "member"}],
            "relationships": [],
        },
        "turns": [{
            "turn_id": "t1",
            "text": "The project meeting is Monday in North Hall.",
            "speaker": {"principal_id": "owner", "role": "member"},
            "turn_kind": "message",
            "timestamp": "2026-01-01T00:00:00Z",
        }],
    }
    checkpoints = [
        {
            "checkpoint_id": "u_fail",
            "episode_id": "episode",
            "as_of_turn_id": "t1",
            "asker": {"principal_id": "owner", "role": "member"},
            "query_text": "When is the meeting?",
            "query_type": "utility",
            "judge_spec": {"include": ["HIDDEN_GOLD"]},
            "leak_targets": ["HIDDEN_LEAK"],
        },
        {
            "checkpoint_id": "u_pass",
            "episode_id": "episode",
            "as_of_turn_id": "t1",
            "asker": {"principal_id": "owner", "role": "member"},
            "query_text": "Where is the meeting?",
            "query_type": "utility",
            "gold_answer_structured": {"secret": "HIDDEN_GOLD"},
        },
        {
            "checkpoint_id": "privacy",
            "episode_id": "episode",
            "as_of_turn_id": "t1",
            "asker": {"principal_id": "owner", "role": "member"},
            "query_text": "May I see private information?",
            "query_type": "privacy",
            "policy_snapshot": {"hidden": "HIDDEN_POLICY"},
        },
        {
            "checkpoint_id": "safety",
            "episode_id": "episode",
            "as_of_turn_id": "t1",
            "asker": {"principal_id": "owner", "role": "member"},
            "query_text": "Recall deleted information",
            "query_type": "safety",
            "attack_type": "HIDDEN_ATTACK",
        },
    ]
    return MedicalDataset([episode], checkpoints)


class WorkflowBackend(Backend):
    """Deterministic behavioral candidates; real build/migration gates still run."""

    def __init__(self, plans, *, crash=False, transport_stage=None):
        super().__init__(mock=True)
        self.plans = list(plans)
        self.architect_calls = []
        self.developer_calls = []
        self.developer_designs = []
        self.correction_calls = []
        self.predict_calls = []
        self.memory_calls = []
        self.workspace_plans = {}
        self.crash = crash
        self.transport_stage = transport_stage

    async def architect(self, context, workspace):
        assert_no_hidden_fields(context)
        self.architect_calls.append(copy.deepcopy(context))
        if self.transport_stage == "architect":
            raise InfrastructureFailure("architect HTTP 503")
        plan = self.plans[min(len(self.architect_calls) - 1, len(self.plans) - 1)]
        if plan.get("charge"):
            self.charge({"total_tokens": plan["charge"]})
        if plan.get("unfocused"):
            return {
                "decision": "CODE_OR_SCHEMA_CHANGE",
                "diagnosis": "too many things",
                "focused_goal": "change everything",
                "work_order": ["change retrieval", "change authorization"],
                "migration_sql": "",
            }
        marker = plan["marker"]
        dossier = context["architect_incident_dossier"]
        trace_id = dossier["trace_entry_ids"][0]
        trace = next(item for target in dossier["targets"]
                     for item in target.get("decision_trace", [])
                     if item["trace_id"] == trace_id)
        cited_id = "missing:trace" if plan.get("invalid_citation") else trace_id
        return {
            "decision": "CODE_OR_SCHEMA_CHANGE",
            "diagnosis": plan.get("diagnosis", f"general mechanism {marker} is missing"),
            "focused_goal": plan.get("goal", f"add general mechanism {marker}"),
            "work_order": [f"Add the general {marker} lookup index."],
            "migration_sql": (
                f"CREATE INDEX IF NOT EXISTS {marker} ON observations(episode_id, turn_id);"
            ),
            "trace_citations": [cited_id],
            "addressed_decision_rule": trace["decision_rule"],
            "predicted_replay_change": {
                "observable": "pass_fail",
                "expected_change": "the unresolved target changes from failing to passing",
            },
        }

    async def develop(self, workspace, design):
        self.developer_calls.append(str(Path(workspace).resolve()))
        self.developer_designs.append(copy.deepcopy(design))
        if self.transport_stage == "developer":
            raise InfrastructureFailure("developer HTTP 503")
        marker = next(item["marker"] for item in self.plans
                      if item.get("marker") and item["marker"] in design.get("migration_sql", ""))
        plan = next(item for item in self.plans if item.get("marker") == marker)
        self.workspace_plans[str(Path(workspace).resolve())] = plan
        if plan.get("unworkable"):
            raise DeveloperApproachUnworkable("focused mechanism cannot preserve authorization")
        await super().develop(workspace, design)
        if plan.get("compile_once"):
            agent = Path(workspace) / "memory_system" / "agent.py"
            plan["original_agent"] = agent.read_text()
            agent.write_text("invalid syntax !")
        if self.crash:
            raise RuntimeError("simulated interruption during candidate development")
        return {"status": "completed", "session": self.developer_calls[-1]}

    async def correct(self, workspace, design, feedback):
        self.correction_calls.append((str(Path(workspace).resolve()), copy.deepcopy(feedback)))
        plan = self.workspace_plans[str(Path(workspace).resolve())]
        if plan.get("original_agent"):
            (Path(workspace) / "memory_system" / "agent.py").write_text(
                plan["original_agent"])
        return {"status": "corrected", "session": self.correction_calls[-1][0]}

    def _marker(self, workspace):
        source = (Path(workspace) / "memory_system" / "schema.sql").read_text()
        found = [plan["marker"] for plan in self.plans
                 if plan.get("marker") and plan["marker"] in source]
        return found[-1] if found else "parent"

    async def memory(self, workspace, blob, header, *, delta=None, checkpoint=None):
        self.memory_calls.append((str(workspace), copy.deepcopy(delta), copy.deepcopy(checkpoint)))
        return await super().memory(workspace, blob, header, delta=delta, checkpoint=checkpoint)

    async def predict(self, workspace, blob, header, checkpoint):
        marker = self._marker(workspace)
        self.predict_calls.append((marker, checkpoint["checkpoint_id"]))
        plan = next((item for item in reversed(self.plans)
                     if item.get("marker") == marker), {})
        if marker == "parent" and self.plans:
            plan = {**plan,
                    "evidence": self.plans[0].get("official_evidence", []),
                    "debug": self.plans[0].get("official_debug", {})}
        answer = plan.get("answers", {}).get(checkpoint["checkpoint_id"])
        if answer is None:
            answer = "bad" if checkpoint["checkpoint_id"] == "u_fail" else "good"
        return {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "output": {
                "action": "answer",
                "answer": answer,
                "retrieved_memory": plan.get("evidence", []),
                "debug": plan.get("debug", {}),
            },
            "render_degraded": False,
            "infrastructure_degraded": False,
        }

    async def judge(self, prediction, checkpoint):
        answer = prediction["output"]["answer"]
        category = checkpoint["query_type"]
        utility = answer == "good"
        privacy = answer == "leak"
        deletion = answer == "deleted"
        passed = utility if category == "utility" else not privacy if category == "privacy" else not deletion
        return {
            "category": category,
            "passed": passed,
            "utility_correct": utility if category == "utility" else None,
            "privacy_leak": privacy if category == "privacy" else None,
            "deletion_leak": deletion if category == "safety" else None,
        }


def fix_plan(marker="fix"):
    return {"marker": marker, "answers": {"u_fail": "good"}}


def make_runner(tmp_path, dataset, backend, **options):
    return OnlineRunner(
        dataset,
        tmp_path / "run",
        backend=backend,
        options=OnlineOptions(**options),
    )


def reports(run):
    return [json.loads(row[0]) for row in run.journal.db.execute(
        "SELECT value FROM attempts ORDER BY boundary,idx")]


def test_all_579_checkpoints_exactly_once_and_549_boundaries():
    if not (config.GATEMEM_DATA_DIR / "checkpoints.jsonl").exists():
        pytest.skip("real medical dataset unavailable")
    data = load_medical_dataset(config.GATEMEM_DATA_DIR)
    events = build_schedule(data)
    ids = [checkpoint_id for event in events for checkpoint_id in event["checkpoint_ids"]]
    assert len(ids) == len(set(ids)) == 579
    assert len(events) == 549


def test_schedule_ignores_annotations_and_file_order(dataset):
    expected = build_schedule(dataset)
    changed = copy.deepcopy(dataset)
    changed.checkpoints.reverse()
    for checkpoint in changed.checkpoints:
        for field in HIDDEN_ANNOTATION_FIELDS:
            checkpoint[field] = "SENTINEL"
    assert build_schedule(changed) == expected


async def test_compile_failure_returns_to_same_developer_candidate(tmp_path, dataset):
    backend = WorkflowBackend([{**fix_plan(), "compile_once": True}])
    run = make_runner(tmp_path, dataset, backend)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        assert len(backend.architect_calls) == 1
        assert len(backend.developer_calls) == len(backend.correction_calls) == 1
        assert backend.developer_calls[0] == backend.correction_calls[0][0]
        assert "compile failed" in backend.correction_calls[0][1]["error"]
        report = reports(run)[0]
        assert report["build_validation"]["status"] == "passed"
        assert report["build_test_errors"][0]["stage"] == "compile"
    finally:
        run.close()


async def test_ordinary_implementation_failure_does_not_recall_architect(tmp_path, dataset):
    backend = WorkflowBackend([{**fix_plan(), "compile_once": True}])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        assert len(backend.architect_calls) == 1
        assert len(backend.correction_calls) == 1
        assert run.summary()["adaptation"]["candidate_rejections"] == 0
    finally:
        run.close()


async def test_unworkable_approach_returns_structured_feedback_to_architect(tmp_path, dataset):
    backend = WorkflowBackend([
        {"marker": "unworkable", "unworkable": True},
        fix_plan("workable"),
    ])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        feedback = backend.architect_calls[1]["previous_rejected_candidate"]
        assert feedback["rejection_reason"] == "approach_unworkable"
        assert feedback["failure_classification"]["tests_never_ran"]
        assert feedback["repair_objective"]
        assert feedback["safe_parent_version"] == "version_0001"
    finally:
        run.close()


async def test_targeted_replay_precedes_broad_validation(tmp_path, dataset):
    backend = WorkflowBackend([fix_plan()])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        candidate_calls = [checkpoint for marker, checkpoint in backend.predict_calls
                           if marker == "fix"]
        assert candidate_calls[0] == "u_fail"
        assert set(candidate_calls[1:]) == {"u_pass", "privacy", "safety"}
        report = reports(run)[0]
        assert report["targeted_replay"]["status"] == "improved"
        assert report["broad_validation"]["status"] == "passed"
    finally:
        run.close()


async def test_no_targeted_improvement_skips_broad_and_feeds_back(tmp_path, dataset):
    backend = WorkflowBackend([
        {"marker": "noop", "answers": {"u_fail": "bad"}},
        fix_plan("fix_after_noop"),
    ])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        first = reports(run)[0]
        assert first["rejection_reason"] == "no_measurable_improvement"
        assert first["broad_validation"] == {
            "status": "skipped", "reason": "no_measurable_improvement"}
        feedback = backend.architect_calls[1]["previous_rejected_candidate"]
        assert feedback["targeted_replay"]["status"] == "no_improvement"
        assert feedback["broad_validation"]["status"] == "skipped"
        differential = feedback["targeted_replay"]["pairs"]["u_fail"]["differential"]
        assert set(differential) == {
            "action", "rendered_answer", "authorized_evidence", "decision_trace", "pass_fail"}
        assert differential["pass_fail"]["transition"] == "unchanged_fail"
        assert feedback["failure_classification"]["ineffective_design"]
    finally:
        run.close()


async def test_failing_to_passing_target_triggers_broad_and_promotion(tmp_path, dataset):
    backend = WorkflowBackend([fix_plan()])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        report = reports(run)[0]
        assert report["targeted_replay"]["failing_to_passing"] == ["u_fail"]
        assert report["broad_validation"]["status"] == "passed"
        assert run.committed != "version_0001"
    finally:
        run.close()


@pytest.mark.parametrize(
    ("checkpoint_id", "bad_answer", "expected"),
    [("privacy", "leak", "privacy"),
     ("safety", "deleted", "deletion"),
     ("u_pass", "bad", "utility")],
)
async def test_target_gain_with_broad_regression_is_rejected_and_fed_back(
    tmp_path, dataset, checkpoint_id, bad_answer, expected,
):
    backend = WorkflowBackend([
        {"marker": "regress", "answers": {"u_fail": "good", checkpoint_id: bad_answer}},
        fix_plan("safe"),
    ])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        first = reports(run)[0]
        assert first["targeted_replay"]["status"] == "improved"
        assert first["broad_validation"]["status"] == "failed"
        assert first["rejection_reason"] == "safety_regression"
        feedback = backend.architect_calls[1]["previous_rejected_candidate"]
        assert expected in {"privacy", "deletion", "utility"}
        assert checkpoint_id in feedback["regressions"]["checkpoint_transitions"]
        assert "metrics" not in feedback["broad_validation"]
        assert run.committed != "version_0001"
    finally:
        run.close()


async def test_target_gain_with_isolation_regression_is_rejected_and_fed_back(
    tmp_path, dataset,
):
    class IsolationRegressionBackend(WorkflowBackend):
        async def memory(self, workspace, blob, header, *, delta=None, checkpoint=None):
            image, result = await super().memory(
                workspace, blob, header, delta=delta, checkpoint=checkpoint)
            if (delta == [] and "attempts" in Path(workspace).parts and
                    self._marker(workspace) == "isolation_bad"):
                path = tmp_path / "isolation.sqlite"
                path.write_bytes(image)
                with sqlite3.connect(path) as database:
                    columns = [row[1] for row in database.execute(
                        "PRAGMA table_info(memories)")]
                    memory = list(database.execute(
                        "SELECT * FROM memories LIMIT 1").fetchone())
                    memory[columns.index("memory_id")] = "isolation_intruder"
                    memory[columns.index("episode_id")] = "another_episode"
                    database.execute(
                        f"INSERT INTO memories VALUES ({','.join('?' for _ in columns)})",
                        memory,
                    )
                image = path.read_bytes()
            return image, result

    backend = IsolationRegressionBackend([
        {**fix_plan("isolation_bad")}, fix_plan("isolation_safe")])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        first = reports(run)[0]
        assert first["targeted_replay"]["status"] == "improved"
        assert first["broad_validation"]["status"] == "failed"
        assert first["rejection_reason"] == "safety_regression"
        feedback = backend.architect_calls[1]["previous_rejected_candidate"]
        assert feedback["regressions"]["isolation"]
    finally:
        run.close()


async def test_rejected_candidate_gets_new_proposal_at_same_boundary(tmp_path, dataset):
    backend = WorkflowBackend([
        {"marker": "noop", "answers": {"u_fail": "bad"}},
        fix_plan("new_design"),
    ])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        assert len(backend.architect_calls) == 2
        assert {row["boundary"] for row in run.journal.db.execute(
            "SELECT boundary FROM attempts")} == {1}
        assert [row[0] for row in run.journal.db.execute(
            "SELECT status FROM attempts ORDER BY idx")] == ["rejected", "promoted"]
    finally:
        run.close()


async def test_runner_does_not_advance_while_incident_unresolved(tmp_path, dataset):
    class ObserveBoundary(WorkflowBackend):
        async def architect(self, context, workspace):
            assert context["incident_report"]["boundary_index"] == 1
            assert run.journal.get("event_boundary_index") == 1
            return await super().architect(context, workspace)

    backend = ObserveBoundary([
        {"marker": "noop", "answers": {"u_fail": "bad"}}, fix_plan("fix")])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        assert run.journal.get("event_boundary_index") == 2
    finally:
        run.close()


async def test_official_results_immutable_and_repairs_separate(tmp_path, dataset):
    backend = WorkflowBackend([fix_plan()])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        official = run.journal.official()
        assert next(row for row in official if row["checkpoint_id"] == "u_fail")["score"]["passed"] is False
        repairs = [json.loads(row[0]) for row in run.journal.db.execute("SELECT value FROM repairs")]
        assert any(row["checkpoint_id"] == "u_fail" and row["score"]["passed"] for row in repairs)
        assert cumulative(official + repairs) == cumulative(official)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            run.journal.db.execute("UPDATE official SET score='{}'")
        run.journal.db.rollback()
    finally:
        run.close()


async def test_large_usage_is_observed_but_never_terminates_repair(tmp_path, dataset):
    backend = WorkflowBackend([
        {"marker": "noop1", "answers": {"u_fail": "bad"}, "charge": 2_000_000},
        {"marker": "noop2", "answers": {"u_fail": "bad"}, "charge": 2_000_000},
        {**fix_plan("eventual"), "charge": 2_000_000},
    ])
    run = make_runner(tmp_path, dataset, backend)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        assert result["total_tokens"] == 6_000_000
        assert len(backend.architect_calls) == 3
        final_context = backend.architect_calls[-1]
        assert final_context["latest_complete_structured_feedback"]["rejection_reason"] == (
            "duplicate_replay_behavior")
        assert len(final_context["earlier_attempt_summaries"]) == 1
        assert final_context["earlier_attempt_summaries"][0]["rejection_reason"] == (
            "no_measurable_improvement")
    finally:
        run.close()


async def test_equivalent_approach_skips_developer_without_ending_loop(tmp_path, dataset):
    duplicate = {"marker": "noop", "answers": {"u_fail": "bad"},
                 "diagnosis": "same diagnosis", "goal": "same goal"}
    backend = WorkflowBackend([duplicate, copy.deepcopy(duplicate), fix_plan("different")])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        rows = reports(run)
        assert rows[1]["rejection_reason"] == "equivalent_previous_attempt"
        assert rows[1]["developer_skipped"]
        assert rows[1]["structured_feedback"]["failure_classification"]["duplicate_proposal"]
        assert len(backend.developer_calls) == 2
        assert rows[-1]["promotion_eligible"]
    finally:
        run.close()


async def test_transport_failure_blocks_same_boundary_without_design_feedback(tmp_path, dataset):
    backend = WorkflowBackend([fix_plan()], transport_stage="developer")
    run = make_runner(tmp_path, dataset, backend, max_infra_retries=1)
    try:
        with pytest.raises(InfrastructureFailure):
            await run.run()
        assert run.journal.get("event_boundary_index") == 1
        incident = run.journal.get("active_repair_incident")
        assert incident["phase"] == "infrastructure_blocked"
        assert incident["structured_feedback_history"] == []
        assert run.committed == "version_0001"
    finally:
        run.close()


async def test_hidden_annotations_never_cross_repair_surfaces(tmp_path, dataset):
    backend = WorkflowBackend([
        {"marker": "noop", "answers": {"u_fail": "bad"}}, fix_plan("fix")])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        for context in backend.architect_calls:
            assert_no_hidden_fields(context)
            encoded = json.dumps(context)
            assert "HIDDEN_GOLD" not in encoded
            assert "HIDDEN_LEAK" not in encoded
            assert "HIDDEN_POLICY" not in encoded
            assert "HIDDEN_ATTACK" not in encoded
        for report in reports(run):
            assert_no_hidden_fields(report)
            assert "HIDDEN_" not in json.dumps(report)
        for design in backend.developer_designs:
            assert_no_hidden_fields(design)
            assert "HIDDEN_" not in json.dumps(design)
    finally:
        run.close()


async def test_scored_failure_persists_privacy_minimized_incident_dossier(tmp_path, dataset):
    selected = {
        "record_id": "mem_allowed", "source_observation_id": "obs_allowed",
        "text": "Authorized Monday context", "status": "active", "role": "member",
    }
    plan = {
        **fix_plan("dossier_fix"),
        "official_evidence": [selected],
        "official_debug": {
            "query_facets": ["date_expression"],
            "supported_query_facets": [],
            "unsupported_query_facets": ["date_expression"],
            "permission_facts": [{
                "permission_id": "perm_1", "effect": "allow", "scope": ["date"],
                "exclusions": [], "status": "active", "valid_from": "2026-01-01",
                "valid_until": "2026-12-31", "valid_at_query": True,
                "query_time": "2026-02-01", "source_observation_id": "obs_perm",
            }],
            "decision_trace": [{
                "trace_id": "u_fail:record:mem_allowed", "record_id": "mem_allowed",
                "source_observation_id": "obs_allowed", "status": "active",
                "classification": "derived_fact", "structural_concepts": ["date_expression"],
                "event_time_features": ["monday"], "outcome": "selected",
                "decision_stage": "result_selection", "decision_rule": "coverage_selected",
                "permission_ids": ["perm_1"],
            }, {
                "trace_id": "u_fail:record:mem_denied", "record_id": "mem_denied",
                "source_observation_id": "obs_denied", "status": "active",
                "classification": "derived_fact", "structural_concepts": ["date_expression"],
                "event_time_features": ["2026-08-01"], "outcome": "denied",
                "decision_stage": "authorization_gate",
                "decision_rule": "event_date_after_permission_valid_until",
                "permission_ids": ["perm_1"],
                "raw_text": "HIDDEN_PATIENT_RAW",
            }],
        },
    }
    backend = WorkflowBackend([plan])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        incident = run.journal.get("repair_incidents")[0]
        dossier = incident["architect_incident_dossier"]
        path = tmp_path / "run/events/boundary_0001/architect_incident_dossier.json"
        assert path.is_file() and json.loads(path.read_text()) == dossier
        target = dossier["targets"][0]
        assert target["authorized_evidence_released"] == [selected]
        denied = next(item for item in target["decision_trace"]
                      if item["record_id"] == "mem_denied")
        assert denied["decision_rule"] == "event_date_after_permission_valid_until"
        assert denied["event_time_features"] == ["2026-08-01"]
        assert "raw_text" not in denied
        assert "HIDDEN_PATIENT_RAW" not in json.dumps(dossier)
        assert backend.architect_calls[0]["architect_incident_dossier"] == dossier
    finally:
        run.close()


async def test_invalid_trace_citation_is_rejected_before_developer_and_fed_back(
    tmp_path, dataset,
):
    backend = WorkflowBackend([
        {"marker": "bad_citation", "invalid_citation": True,
         "answers": {"u_fail": "bad"}},
        fix_plan("valid_citation"),
    ])
    run = make_runner(tmp_path, dataset, backend)
    try:
        await run.run()
        first = reports(run)[0]
        assert first["rejection_reason"] == "diagnosis_review_rejected"
        assert first["diagnosis_review"]["checks"]["cited_traces_exist"] is False
        assert first["structured_feedback"]["failure_classification"][
            "diagnosis_review_rejection"]
        assert first["developer_skipped"]
        assert len(backend.developer_calls) == 1
        context = backend.architect_calls[1]
        feedback = context["latest_complete_structured_feedback"]
        assert feedback["diagnosis_review"]["feedback"]
        assert feedback == context["previous_rejected_candidate"]
    finally:
        run.close()


async def test_resume_preserves_dossier_review_and_retry_state(tmp_path, dataset):
    crashing = WorkflowBackend([
        {"marker": "review_reject", "invalid_citation": True,
         "answers": {"u_fail": "bad"}},
        fix_plan("crashing_candidate"),
    ], crash=True)
    run = make_runner(tmp_path, dataset, crashing)
    options = run.options
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await run.run()
    incident_before = run.journal.get("active_repair_incident")
    dossier_before = incident_before["architect_incident_dossier"]
    assert [item["status"] for item in incident_before["diagnosis_review_history"]] == [
        "rejected", "accepted"]
    run.close()

    resumed_backend = WorkflowBackend([fix_plan("resumed_after_review")])
    resumed = OnlineRunner(
        dataset, tmp_path / "run", backend=resumed_backend, options=options, resume=True)
    try:
        active = resumed.journal.get("active_repair_incident")
        assert active["architect_incident_dossier"] == dossier_before
        assert len(active["diagnosis_review_history"]) == 2
        await resumed.run()
        statuses = [row[0] for row in resumed.journal.db.execute(
            "SELECT status FROM attempts ORDER BY idx")]
        assert statuses == ["rejected", "interrupted", "promoted"]
    finally:
        resumed.close()


def test_trace_separates_permission_time_from_event_date_and_cancelled_status(tmp_path):
    store = MemoryStore(tmp_path / "trace.sqlite")
    try:
        permission_source = store.ingest_turn("episode", {
            "turn_id": "permission", "timestamp": "2026-05-01T00:00:00Z",
            "speaker": {"principal_id": "owner", "role": "member"},
            "text": "permission source", "turn_kind": "message",
        }, "owner")
        event_source = store.ingest_turn("episode", {
            "turn_id": "event", "timestamp": "2026-05-02T00:00:00Z",
            "speaker": {"principal_id": "owner", "role": "member"},
            "text": "event source", "turn_kind": "message",
        }, "owner")
        store.grant_permission(
            "episode", grantee_id="requester", resource_owner_id="owner",
            scope=["appointment date"], source_observation_id=permission_source,
            valid_from="2026-01-01", valid_until="2026-06-01")
        future = store.add_fact(
            "episode", subject_id="owner", predicate="appointment", value={},
            text_value="appointment 2026-08-01", concepts=["appointment", "date_expression"],
            source_observation_id=event_source)
        cancelled = store.add_fact(
            "episode", subject_id="owner", predicate="appointment", value={},
            text_value="appointment cancelled monday", concepts=["appointment", "date_expression"],
            status="cancelled", source_observation_id=event_source)
        denied = store.retrieve(
            requester_id="requester", owner_id="owner", episode_id="episode",
            query="appointment date", checkpoint_id="cp", top_k=12)
        future_trace = next(item for item in denied.decision_trace
                            if item["record_id"] == future)
        # Consent valid at query time releases a later-dated event; the
        # permission's expiry bounds when it can be asked, not what it covers.
        assert future_trace["outcome"] == "selected"
        assert future in [item.record_id for item in denied.allowed]
        assert future_trace["event_time_features"] == ["2026-08-01"]
        assert future_trace["permission_validity_at_query"]["query_time"].startswith("2026-05-02")
        assert denied.permission_facts[0]["valid_until"] == "2026-06-01"
        assert denied.permission_facts[0]["valid_at_query"] is True

        store.ingest_turn("episode", {
            "turn_id": "later", "timestamp": "2026-07-01T00:00:00Z",
            "speaker": {"principal_id": "owner", "role": "member"},
            "text": "later boundary", "turn_kind": "message",
        }, "owner")
        expired = store.retrieve(
            requester_id="requester", owner_id="owner", episode_id="episode",
            query="appointment date", checkpoint_id="expired_cp", top_k=12)
        assert expired.permission_facts[0]["valid_at_query"] is False
        future_after_expiry = next(item for item in expired.decision_trace
                                   if item["record_id"] == future)
        assert future_after_expiry["permission_validity_at_query"][
            "active_permission_ids"] == []

        owner_view = store.retrieve(
            requester_id="owner", owner_id="owner", episode_id="episode",
            query="appointment date", checkpoint_id="owner_cp", top_k=12)
        cancelled_trace = next(item for item in owner_view.decision_trace
                               if item["record_id"] == cancelled)
        assert cancelled_trace["status"] == "cancelled"
        assert cancelled_trace["outcome"] == "selected"
        assert cancelled not in owner_view.obsolete
    finally:
        store.close()


async def test_resume_marks_interrupted_candidate_and_keeps_boundary_and_official(tmp_path, dataset):
    crashing = WorkflowBackend([fix_plan()], crash=True)
    run = make_runner(tmp_path, dataset, crashing)
    options = run.options
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await run.run()
    before = run.journal.official()
    assert run.journal.get("event_boundary_index") == 1
    run.close()

    resumed_backend = WorkflowBackend([fix_plan("resume_fix")])
    resumed = OnlineRunner(
        dataset, tmp_path / "run", backend=resumed_backend, options=options, resume=True)
    try:
        await resumed.run()
        assert resumed.journal.official() == before
        assert [row[0] for row in resumed.journal.db.execute(
            "SELECT status FROM attempts ORDER BY idx")] == ["interrupted", "promoted"]
        assert resumed.journal.get("event_boundary_index") == 2
        assert resumed.journal.db.execute("SELECT cursor FROM episodes").fetchone()[0] == 0
    finally:
        resumed.close()


def test_batch_developer_budgets_remain_unchanged():
    assert _stop_reason({"retry_count": config.MAX_DEV_RETRIES}) == "exhausted"
    assert _stop_reason({"retry_count": config.MAX_DEV_RETRIES,
                         "online_unbounded": True}) == ""
    online = _turn_budget_block({"online_unbounded": True}, 100_000)
    assert "no online turn ceiling" in online
    assert config.MAX_TOTAL_TOKENS > 0 and config.MAX_WALLCLOCK_S > 0


def test_visible_checkpoint_allowlist_excludes_unknown_hidden_fields(dataset):
    checkpoint = {
        **dataset.checkpoints[0],
        "gold_answer": "UNKNOWN_GOLD",
        "future_checkpoints": [{"query_text": "FUTURE"}],
    }
    visible = visible_checkpoint(checkpoint)
    assert set(visible) == {
        "checkpoint_id", "episode_id", "as_of_turn_id", "asker", "query_text"}
    assert "UNKNOWN" not in json.dumps(visible) and "FUTURE" not in json.dumps(visible)


def test_structural_compatibility_contains_no_turn_text():
    manifest = {"schema_version": 1,
                "supported_turn_fields": ["turn_id", "text", "speaker"]}
    summary = structural_summary([{"turn_id": "a", "text": "DO_NOT_EXPOSE", "speaker": {}}])
    assert "DO_NOT_EXPOSE" not in json.dumps(summary)
    assert compatibility(summary, manifest)["decision"] == "NO_CHANGE"


def _database_blob(tmp_path):
    path = tmp_path / "example.sqlite"
    with sqlite3.connect(path) as database:
        database.executescript(
            "CREATE TABLE facts(id INTEGER PRIMARY KEY, body TEXT);"
            "INSERT INTO facts VALUES(1, 'preserved');")
    return path.read_bytes()


def test_migration_transactional_idempotent_and_data_preserving(tmp_path):
    blob = _database_blob(tmp_path)
    migrated = migrate_blob(blob, "CREATE INDEX IF NOT EXISTS fact_idx ON facts(body);")
    assert migrate_blob(migrated, "CREATE INDEX IF NOT EXISTS fact_idx ON facts(body);") == migrated
    with pytest.raises(sqlite3.Error):
        migrate_blob(blob, "CREATE TABLE partly(x); INSERT INTO absent VALUES (1);")
    with pytest.raises(ValidationFailure, match="data_preservation"):
        migrate_blob(blob, "DELETE FROM facts;")


@pytest.mark.parametrize("field,category", [
    ("privacy_leak", "privacy"), ("deletion_leak", "safety")])
def test_privacy_and_deletion_regressions_are_hard_failures(field, category):
    old = {"x": {"category": category, field: False}}
    new = {"x": {"category": category, field: True}}
    assert promotion_regressions(old, new, tolerance=1.0)


def test_utility_tolerance():
    old = {str(index): {"category": "utility", "utility_correct": True}
           for index in range(10)}
    new = copy.deepcopy(old)
    new["0"]["utility_correct"] = False
    assert promotion_regressions(old, new, tolerance=0.09)
    assert not promotion_regressions(old, new, tolerance=0.10)


def resume_runner(tmp_path, dataset, backend, **flags):
    options = flags.pop("options", {})
    return OnlineRunner(dataset, tmp_path / "run", backend=backend,
                        options=OnlineOptions(**options), resume=True, **flags)


async def test_stalled_incident_blocks_durably_then_abandon_advances(tmp_path, dataset):
    limits = {"max_stalled_attempts": 3, "max_duplicate_proposals": 99}
    backend = WorkflowBackend([{"marker": f"noop{i}", "answers": {"u_fail": "bad"},
                                "diagnosis": f"idea {i}", "goal": f"goal {i}"}
                               for i in range(1, 6)])
    run = make_runner(tmp_path, dataset, backend, **limits)
    try:
        with pytest.raises(RepairBlocked, match="3 consecutive rejected candidates"):
            await run.run()
        assert len(backend.architect_calls) == 3
        assert run.journal.get("status") == "repair_blocked"
        assert run.journal.get("event_boundary_index") == 1
        assert run.journal.get("active_repair_incident")["phase"] == "stalled"
    finally:
        run.close()

    blocked = resume_runner(tmp_path, dataset, WorkflowBackend([fix_plan()]), options=limits)
    try:
        with pytest.raises(RepairBlocked):
            await blocked.run()  # neither flag: stays blocked, nothing proposed
        assert blocked.journal.get("event_boundary_index") == 1
    finally:
        blocked.close()

    resumed = resume_runner(tmp_path, dataset, WorkflowBackend([fix_plan()]),
                            options=limits, abandon_blocked=True)
    try:
        result = await resumed.run()
        assert result["status"] == "complete"
        assert result["adaptation"]["abandoned_failures"] == ["u_fail"]
        assert result["adaptation"]["pending_repairs"] == 0
        official = {row["checkpoint_id"]: row for row in resumed.journal.official()}
        assert official["u_fail"]["score"]["passed"] is False
        assert resumed.journal.get("repair_incidents")[0]["resolution"] == "abandoned_after_stall"
    finally:
        resumed.close()


async def test_retry_blocked_resets_stall_count_and_repairs(tmp_path, dataset):
    limits = {"max_stalled_attempts": 2, "max_duplicate_proposals": 99}
    backend = WorkflowBackend([{"marker": f"noop{i}", "answers": {"u_fail": "bad"},
                                "diagnosis": f"idea {i}", "goal": f"goal {i}"}
                               for i in range(1, 4)])
    run = make_runner(tmp_path, dataset, backend, **limits)
    try:
        with pytest.raises(RepairBlocked):
            await run.run()
    finally:
        run.close()
    resumed = resume_runner(tmp_path, dataset, WorkflowBackend([fix_plan("after_retry")]),
                            options=limits, retry_blocked=True)
    try:
        result = await resumed.run()
        assert result["status"] == "complete"
        assert resumed.committed != "version_0001"
        assert result["adaptation"]["abandoned_failures"] == []
    finally:
        resumed.close()


async def test_duplicate_proposals_block_after_limit(tmp_path, dataset):
    duplicate = {"marker": "noop", "answers": {"u_fail": "bad"},
                 "diagnosis": "same diagnosis", "goal": "same goal"}
    backend = WorkflowBackend([duplicate])
    run = make_runner(tmp_path, dataset, backend, max_stalled_attempts=99)
    try:
        with pytest.raises(RepairBlocked, match="duplicated earlier rejected work"):
            await run.run()
        rows = reports(run)
        assert [row["rejection_reason"] for row in rows] == [
            "no_measurable_improvement"] + ["equivalent_previous_attempt"] * 3
        assert len(backend.developer_calls) == 1
    finally:
        run.close()


class NeverCorrects(WorkflowBackend):
    async def correct(self, workspace, design, feedback):
        self.correction_calls.append((str(Path(workspace).resolve()), copy.deepcopy(feedback)))
        return {"status": "still broken"}


async def test_build_failure_stops_after_correction_limit(tmp_path, dataset):
    backend = NeverCorrects([{**fix_plan("broken"), "compile_once": True}, fix_plan("fixed")])
    run = make_runner(tmp_path, dataset, backend, max_correction_rounds=3)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        first = reports(run)[0]
        assert first["rejection_reason"] == "implementation_failure"
        assert first["build_validation"]["status"] == "failed_after_correction_limit"
        assert len(backend.correction_calls) == 3
        assert "3 correction round(s)" in first["structured_feedback"]["rejection_detail"]
    finally:
        run.close()


async def test_mock_architect_reports_no_viable_repair_and_blocks(tmp_path, dataset):
    run = make_runner(tmp_path, dataset, Backend(mock=True))
    try:
        with pytest.raises(RepairBlocked, match="no viable focused repair"):
            await run.run()
        rows = reports(run)
        assert [row["rejection_reason"] for row in rows] == [
            "no_measurable_improvement", "architect_no_viable_repair"]
        assert run.journal.get("status") == "repair_blocked"
    finally:
        run.close()


class FlakyJudge(WorkflowBackend):
    """The candidate's correct answer to u_fail is judged fail, pass, pass, ..."""

    def __init__(self, plans):
        super().__init__(plans)
        self.good_u_fail_judgements = 0

    async def judge(self, prediction, checkpoint):
        score = await super().judge(prediction, checkpoint)
        if checkpoint["checkpoint_id"] == "u_fail" and prediction["output"]["answer"] == "good":
            self.good_u_fail_judgements += 1
            passed = self.good_u_fail_judgements != 1
            score = {**score, "passed": passed, "utility_correct": passed}
        return score


async def test_one_noisy_judgement_rejects_a_fix_without_votes(tmp_path, dataset):
    run = make_runner(tmp_path, dataset, FlakyJudge([fix_plan()]))
    try:
        with pytest.raises(RepairBlocked):
            await run.run()
        assert reports(run)[0]["rejection_reason"] == "no_measurable_improvement"
    finally:
        run.close()


async def test_replay_votes_take_the_majority(tmp_path, dataset):
    backend = FlakyJudge([fix_plan()])
    run = make_runner(tmp_path, dataset, backend, replay_votes=3)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        pair = reports(run)[0]["targeted_replay"]["pairs"]["u_fail"]
        assert pair["transition"] == "failing_to_passing"
        assert backend.good_u_fail_judgements == 3
    finally:
        run.close()


async def test_reingest_rebuilds_both_sides_from_turns_and_promotes(tmp_path, dataset):
    backend = WorkflowBackend([fix_plan()])
    run = make_runner(tmp_path, dataset, backend, replay_reingest=True)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        rebuilt = {Path(workspace).parent.name if "attempt_" in workspace else "parent"
                   for workspace, delta, _ in backend.memory_calls
                   if delta and [turn["turn_id"] for turn in delta] == ["t1"]}
        assert {"parent", "attempt_01"} <= rebuilt
    finally:
        run.close()


class DropsDeletion(WorkflowBackend):
    async def develop(self, workspace, design):
        outcome = await super().develop(workspace, design)
        if "forgetful" in design.get("migration_sql", ""):
            store = Path(workspace) / "memory_system" / "store.py"
            # Subtle enough to pass the trusted tests: a deletion request now
            # needs four shared terms instead of two to tombstone a turn.
            store.write_text(store.read_text().replace(
                'and len(terms & _concepts(r["raw_text"])) >= 2]',
                'and len(terms & _concepts(r["raw_text"])) >= 4]'))
        return outcome


async def test_reingest_rejects_candidate_that_loses_a_tombstone(tmp_path, dataset):
    dataset.episodes[0]["turns"].append({
        "turn_id": "t2", "text": "Please delete the North Hall meeting note.",
        "speaker": {"principal_id": "owner", "role": "member"},
        "turn_kind": "message", "timestamp": "2026-01-02T00:00:00Z",
    })
    for checkpoint in dataset.checkpoints:
        checkpoint["as_of_turn_id"] = "t2"
    backend = DropsDeletion([fix_plan("forgetful"), fix_plan("faithful")])
    run = make_runner(tmp_path, dataset, backend, replay_reingest=True)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        first, second = reports(run)
        assert first["rejection_reason"] == "safety_regression"
        assert "lost tombstones" in first["structured_feedback"]["rejection_detail"]
        assert second["promotion_eligible"]
    finally:
        run.close()


async def test_missing_sandbox_blocks_as_infrastructure_not_as_an_ingestion_incident(
        tmp_path, dataset, monkeypatch):
    import online.validation
    monkeypatch.setattr(online.validation.shutil, "which", lambda name: None)
    backend = WorkflowBackend([fix_plan()])
    run = make_runner(tmp_path, dataset, backend)
    try:
        with pytest.raises(InfrastructureFailure, match="ingest: transport retry limit"):
            await run.run()
        assert run.journal.get("status") == "infrastructure_blocked"
        assert run.journal.get("repair_incidents") == []
        assert backend.architect_calls == []
    finally:
        run.close()
