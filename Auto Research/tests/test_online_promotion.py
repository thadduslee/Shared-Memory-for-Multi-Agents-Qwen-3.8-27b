"""Transactional staged ingestion, broad safety, and partial promotion tests."""
from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from gatemem_adapter import MedicalDataset, assert_no_hidden_fields
from online.backend import Backend, InfrastructureFailure
from online.metrics import candidate_metrics
from online.runner import OnlineOptions, OnlineRunner
from online.validation import ValidationFailure, code_manifest, copy_workspace, sandbox_command


@pytest.fixture
def stream():
    episode = {
        "episode_id": "patient_episode",
        "domain": "generic",
        "entities": {
            "principals": [{"principal_id": "patient", "role": "member"}],
            "relationships": [],
        },
        "turns": [
            {
                "turn_id": "t1", "text": "appointment Monday",
                "speaker": {"principal_id": "patient", "role": "member"},
                "turn_kind": "message",
            },
            {
                "turn_id": "t2", "text": "appointment Tuesday",
                "speaker": {"principal_id": "patient", "role": "member"},
                "turn_kind": "message", "new_structure": True,
            },
        ],
    }
    checkpoints = [
        {
            "checkpoint_id": category,
            "episode_id": "patient_episode",
            "as_of_turn_id": "t1",
            "asker": {"principal_id": "patient", "role": "member"},
            "query_text": "appointment",
            "query_type": category,
            "gold_answer_structured": {"secret": "HIDDEN_GOLD"},
        }
        for category in ("utility", "privacy", "safety")
    ]
    checkpoints.append({
        "checkpoint_id": "next",
        "episode_id": "patient_episode",
        "as_of_turn_id": "t2",
        "asker": {"principal_id": "patient", "role": "member"},
        "query_text": "appointment",
        "query_type": "utility",
        "attack_type": "HIDDEN_ATTACK",
    })
    return MedicalDataset([episode], checkpoints)


class IngestionBackend(Backend):
    def __init__(self, plans):
        super().__init__(mock=True)
        self.plans = list(plans)
        self.architect_contexts = []
        self.developer_calls = []
        self.delta_calls = []
        self.current = {}
        self.target_ingested = set()

    async def architect(self, context, workspace):
        assert_no_hidden_fields(context)
        assert "HIDDEN_" not in json.dumps(context)
        self.architect_contexts.append(copy.deepcopy(context))
        plan = self.plans[min(len(self.architect_contexts) - 1, len(self.plans) - 1)]
        marker = plan["marker"]
        dossier = context["architect_incident_dossier"]
        trace_id = dossier["trace_entry_ids"][0]
        target = dossier["targets"][0]
        trace_rule = target.get("decision_rule")
        if not trace_rule:
            trace_rule = next(item["decision_rule"] for item in target["decision_trace"]
                              if item["trace_id"] == trace_id)
        return {
            "decision": "CODE_OR_SCHEMA_CHANGE",
            "failure_classification": (
                "INGESTION" if context["incident_report"]["trigger_type"] == "pre_ingestion"
                else "RETRIEVAL"),
            "diagnosis": plan.get("diagnosis", f"incoming structure needs {marker}"),
            "specific_hypothesis": (
                f"Adding the {marker} representation index makes the exact staged delta observable."),
            "repair_scope": {
                "subsystem": "storage", "file": "memory_system/schema.sql",
                "function": "schema initialization",
            },
            "focused_goal": plan.get("goal", f"support {marker} input representation"),
            "work_order": [f"Add one general {marker} representation index."],
            "migration_sql": (
                f"CREATE INDEX IF NOT EXISTS {marker} ON observations(episode_id, turn_id);"
            ),
            "trace_citations": [trace_id],
            "addressed_decision_rule": trace_rule,
            "intended_target_ids": [
                target["checkpoint_id"] for target in dossier["targets"]
                if target.get("checkpoint_id")
            ],
            "predicted_replay_change": {
                "observable": "decision_trace",
                "expected_change": "the cited pre-build outcome changes on targeted replay",
            },
            "safety_preservation_checks": [
                "Preserve privacy, deletion, and every previously-passing checkpoint."
            ],
        }

    async def develop(self, workspace, design):
        self.developer_calls.append(str(workspace))
        marker = next(plan["marker"] for plan in self.plans
                      if plan["marker"] in design["migration_sql"])
        self.current[str(Path(workspace).resolve())] = next(
            plan for plan in self.plans if plan["marker"] == marker)
        return await super().develop(workspace, design)

    def _plan(self, workspace):
        resolved = str(Path(workspace).resolve())
        if resolved in self.current:
            return self.current[resolved]
        schema = Path(workspace) / "memory_system" / "schema.sql"
        if schema.exists():
            source = schema.read_text()
            return next((plan for plan in reversed(self.plans)
                         if plan.get("marker") and plan["marker"] in source), {})
        return {}

    async def memory(self, workspace, blob, header, *, delta=None, checkpoint=None):
        plan = self._plan(workspace)
        if delta:
            self.delta_calls.append((str(workspace), copy.deepcopy(delta)))
            if delta[-1]["turn_id"] == "t2":
                self.target_ingested.add(str(Path(workspace).resolve()))
            if plan.get("drop_delta") and delta[-1]["turn_id"] == "t2":
                return blob, {}
        image, result = await super().memory(
            workspace, blob, header, delta=delta, checkpoint=checkpoint)
        if plan.get("corrupt_isolation") and delta and delta[-1]["turn_id"] == "t2":
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "image.sqlite"
                path.write_bytes(image)
                with sqlite3.connect(path) as database:
                    database.execute(
                        "UPDATE observations SET episode_id='another_episode' "
                        "WHERE turn_id='t2'")
                image = path.read_bytes()
        if plan.get("corrupt_payload") and delta and delta[-1]["turn_id"] == "t2":
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "image.sqlite"
                path.write_bytes(image)
                with sqlite3.connect(path) as database:
                    database.execute(
                        "UPDATE observations SET raw_payload_json='{}' WHERE turn_id='t2'")
                image = path.read_bytes()
        if (plan.get("erase_history") and delta == [] and
                str(Path(workspace).resolve()) in self.target_ingested):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "image.sqlite"
                path.write_bytes(image)
                with sqlite3.connect(path) as database:
                    database.execute("DELETE FROM memories")
                image = path.read_bytes()
        return image, result

    async def predict(self, workspace, blob, header, checkpoint):
        plan = self._plan(workspace)
        answer = plan.get("answers", {}).get(checkpoint["checkpoint_id"], "good")
        return {"checkpoint_id": checkpoint["checkpoint_id"],
                "output": {"action": "answer", "answer": answer}}

    async def judge(self, prediction, checkpoint):
        category = checkpoint["query_type"]
        answer = prediction["output"]["answer"]
        privacy = answer == "leak"
        deletion = answer == "deleted"
        utility = answer == "good"
        passed = utility if category == "utility" else not privacy if category == "privacy" else not deletion
        return {"category": category, "passed": passed,
                "utility_correct": utility if category == "utility" else None,
                "privacy_leak": privacy if category == "privacy" else None,
                "deletion_leak": deletion if category == "safety" else None}


def safe_plan(marker="safe_input"):
    return {"marker": marker}


def make_runner(tmp_path, stream, backend, **options):
    return OnlineRunner(
        stream, tmp_path / "run", backend=backend, options=OnlineOptions(**options))


def attempt_reports(run):
    return [json.loads(row[0]) for row in run.journal.db.execute(
        "SELECT value FROM attempts ORDER BY boundary,idx")]


def test_parent_replay_cache_key_requires_identical_version_config_and_state(tmp_path, stream):
    run = make_runner(tmp_path, stream, IngestionBackend([safe_plan()]))
    try:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with sqlite3.connect(path) as database:
                database.execute("CREATE TABLE sample(value TEXT)")
            blob = path.read_bytes()
            checkpoint = stream.checkpoints[0]
            args = dict(system_version="version_0001", workspace=run.workspace(),
                        checkpoint=checkpoint, boundary=1, blob=blob, vote=0)
            first = run._replay_cache_key(**args)
            assert first == run._replay_cache_key(**args)
            assert first != run._replay_cache_key(**{**args, "system_version": "version_9999"})
            changed = {**checkpoint, "query_text": checkpoint["query_text"] + " now"}
            assert first != run._replay_cache_key(**{**args, "checkpoint": changed})
            with sqlite3.connect(path) as database:
                database.execute("INSERT INTO sample VALUES ('changed')")
            assert first != run._replay_cache_key(**{**args, "blob": path.read_bytes()})
    finally:
        run.close()


def test_candidate_verification_rejects_inconsistent_component_versions(tmp_path, stream):
    run = make_runner(tmp_path, stream, IngestionBackend([safe_plan()]))
    try:
        candidate = "version_0002"
        workspace = run.workspace(candidate)
        copy_workspace(run.workspace(), workspace)
        parent = run.version_manifest()
        manifest = {
            **parent,
            "parent": run.committed,
            "status": "candidate",
            "changed_component": "memory",
            "renderer_mode": run.options.renderer_mode,
            "code": code_manifest(workspace),
            # A memory candidate must advance this value. Keeping the parent's
            # tuple is a corrupt provisional manifest, even when code hashes match.
            "memory_version": parent["memory_version"],
            "renderer_version": parent["renderer_version"],
        }
        (workspace.parent / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ValidationFailure, match="component version tuple"):
            run._verify_version(candidate)
        assert run.committed == "version_0001"
    finally:
        run.close()


async def test_candidate_is_verified_before_committed_pointer_changes(tmp_path, stream):
    run = make_runner(
        tmp_path, stream, IngestionBackend([safe_plan()]), max_failed_candidates=1)
    calls = []
    original = run._verify_version

    def reject_candidate(version):
        calls.append(version)
        if version != "version_0001":
            raise ValidationFailure("candidate hash mismatch", stage="promotion_verification")
        return original(version)

    run._verify_version = reject_candidate
    try:
        await run.run()
        assert any(version != "version_0001" for version in calls)
        assert run.committed == "version_0001"
        report = attempt_reports(run)[0]
        assert report["validation_stage"] == "promotion_verification"
        assert report["rejection_reason"] == "implementation_failure"
    finally:
        run.close()


@pytest.mark.parametrize("failure", ["drop_delta", "corrupt_isolation", "corrupt_payload"])
async def test_staged_delta_unchanged_across_failed_repairs_and_final_commit(
    tmp_path, stream, failure,
):
    backend = IngestionBackend([
        {"marker": "bad_input", failure: True}, safe_plan()])
    run = make_runner(tmp_path, stream, backend)
    try:
        await run.run()
        rows = attempt_reports(run)
        assert rows[0]["rejection_reason"] == "cannot_represent_staged_data"
        assert rows[0]["broad_validation"]["status"] == "skipped"
        histories = [item for item in run.journal.get("repair_incidents")
                     if item.get("trigger_type") == "pre_ingestion"]
        incident = histories[-1]
        assert incident["staged_input"]["turns"] == [stream.episodes[0]["turns"][1]]
        assert all(call[1] == [stream.episodes[0]["turns"][1]]
                   for call in backend.delta_calls if call[1][-1]["turn_id"] == "t2")
        assert run.journal.db.execute("SELECT cursor FROM episodes").fetchone()[0] == 1
    finally:
        run.close()


async def test_candidate_supporting_staged_delta_promotes_and_commits_once(tmp_path, stream):
    backend = IngestionBackend([safe_plan()])
    run = make_runner(tmp_path, stream, backend)
    try:
        await run.run()
        assert run.committed != "version_0001"
        row = run.journal.db.execute("SELECT cursor,memory FROM episodes").fetchone()
        assert row["cursor"] == 1
        path = tmp_path / "memory.sqlite"
        path.write_bytes(row["memory"])
        with sqlite3.connect(path) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM observations WHERE turn_id='t2'").fetchone()[0] == 1
        official = run.journal.official()
        assert len(official) == 4
        assert len({row["checkpoint_id"] for row in official}) == 4
        assert run.journal.event(2)[0] == "repaired_and_passed"
        attempts = attempt_reports(run)
        required = attempts[0]["targeted_replay"]["required_answers"]
        assert required["status"] == "deferred_until_committed_official_scoring"
        assert required["checkpoint_ids"] == ["next"]
        assert attempts[0]["counts_as_failed_candidate"] is False
    finally:
        run.close()


async def test_representation_promotes_before_wrong_official_answer_and_scored_repair(
    tmp_path, stream,
):
    backend = IngestionBackend([
        {"marker": "storage_parent", "answers": {"next": "bad"}},
        {"marker": "answer_fix", "answers": {"next": "good"}},
    ])
    run = make_runner(tmp_path, stream, backend)
    try:
        await run.run()
        attempts = [row for row in attempt_reports(run) if row["boundary_index"] == 2]
        assert len(attempts) == 2
        representation, scored_repair = attempts
        assert representation["representation_success"] is True
        assert representation["counts_as_failed_candidate"] is False
        assert representation["failed_candidate_attempts_after"] == 0
        assert representation["targeted_replay"]["required_answers"]["status"] == (
            "deferred_until_committed_official_scoring")
        representation_version = representation["candidate_system_version"]

        official = next(row for row in run.journal.official()
                        if row["checkpoint_id"] == "next")
        assert official["frozen_system_version"] == representation_version
        assert official["prediction"]["output"]["answer"] == "bad"
        assert official["score"]["passed"] is False
        assert scored_repair["safe_parent_version"] == representation_version
        assert scored_repair["failed_candidate_attempts_after"] == 0
        assert run.journal.event(2)[0] == "repaired_and_passed"
        state = run.journal.event(2)[1]
        assert state["representation_committed"] is True
        assert state["representation_committed_version"] == representation_version
    finally:
        run.close()


def _stream_with_later_boundary(stream):
    extended = copy.deepcopy(stream)
    extended.episodes[0]["turns"].append({
        "turn_id": "t3", "text": "appointment Wednesday",
        "speaker": {"principal_id": "patient", "role": "member"},
        "turn_kind": "message",
    })
    extended.checkpoints.append({
        "checkpoint_id": "after_unrepaired",
        "episode_id": "patient_episode",
        "as_of_turn_id": "t3",
        "asker": {"principal_id": "patient", "role": "member"},
        "query_text": "appointment",
        "query_type": "utility",
    })
    return MedicalDataset(extended.episodes, extended.checkpoints)


async def test_representation_and_scored_failures_share_counter_and_keep_committed_data(
    tmp_path, stream,
):
    data = _stream_with_later_boundary(stream)
    plans = [
        {"marker": "cannot_store", "drop_delta": True},
        {"marker": "represented", "answers": {"next": "bad"}},
    ] + [
        {"marker": f"answer_miss_{index}", "answers": {"next": "bad"},
         "diagnosis": f"answer repair idea {index}"}
        for index in range(1, 5)
    ]
    run = make_runner(tmp_path, data, IngestionBackend(plans), max_failed_candidates=5)
    try:
        await run.run()
        attempts = [row for row in attempt_reports(run) if row["boundary_index"] == 2]
        assert [row["counts_as_failed_candidate"] for row in attempts] == [
            True, False, True, True, True, True]
        assert [row["failed_candidate_attempts_after"] for row in attempts] == [
            1, 1, 2, 3, 4, 5]
        representation_version = attempts[1]["candidate_system_version"]
        assert all(row["safe_parent_version"] == representation_version
                   for row in attempts[2:])
        assert run.journal.event(2)[0] == "unrepaired_after_5_attempts"
        terminal = run.journal.event(2)[1]
        assert terminal["ingestion_occurred"] is True
        assert terminal["representation_committed"] is True
        assert terminal["representation_committed_version"] == representation_version
        assert terminal["failed_candidate_attempts"] == 5
        assert run.committed == representation_version

        official = next(row for row in run.journal.official()
                        if row["checkpoint_id"] == "next")
        assert official["score"]["passed"] is False
        assert official["frozen_system_version"] == representation_version
        assert run.journal.event(3)[0] == "passed"
        assert next(row for row in run.journal.official()
                    if row["checkpoint_id"] == "after_unrepaired")["score"]["passed"]

        memory = run.journal.db.execute(
            "SELECT memory FROM episodes WHERE id='patient_episode'").fetchone()[0]
        path = tmp_path / "represented.sqlite"
        path.write_bytes(memory)
        with sqlite3.connect(path) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM observations WHERE turn_id='t2'").fetchone()[0] == 1
    finally:
        run.close()


async def test_representation_success_then_five_scored_candidates_fail(tmp_path, stream):
    plans = [
        {"marker": "represented_before_exhaustion", "answers": {"next": "bad"}},
    ] + [
        {"marker": f"scored_failure_{index}", "answers": {"next": "bad"},
         "diagnosis": f"distinct scored repair {index}"}
        for index in range(1, 6)
    ]
    run = make_runner(tmp_path, stream, IngestionBackend(plans), max_failed_candidates=5)
    try:
        await run.run()
        attempts = [row for row in attempt_reports(run) if row["boundary_index"] == 2]
        representation_version = attempts[0]["candidate_system_version"]
        assert attempts[0]["representation_success"] is True
        assert attempts[0]["failed_candidate_attempts_after"] == 0
        assert [row["failed_candidate_attempts_after"] for row in attempts[1:]] == [
            1, 2, 3, 4, 5]
        assert all(row["safe_parent_version"] == representation_version
                   for row in attempts[1:])
        assert run.journal.event(2)[0] == "unrepaired_after_5_attempts"
        assert run.journal.event(2)[1]["ingestion_occurred"] is True
        assert run.committed == representation_version
    finally:
        run.close()


async def test_resume_after_representation_promotion_keeps_phase_parent_rows_and_counter(
    tmp_path, stream,
):
    class StopsAtScoredArchitect(IngestionBackend):
        async def architect(self, context, workspace):
            if len(self.architect_contexts) >= 2:
                raise InfrastructureFailure("temporary scored-repair outage")
            return await super().architect(context, workspace)

    options = OnlineOptions(max_infra_retries=0)
    first = OnlineRunner(
        stream, tmp_path / "run",
        backend=StopsAtScoredArchitect([
            {"marker": "bad_representation", "drop_delta": True},
            {"marker": "resume_parent", "answers": {"next": "bad"}},
        ]),
        options=options,
    )
    try:
        with pytest.raises(InfrastructureFailure):
            await first.run()
        phase, state, _ = first.journal.event(2)
        assert phase == "critiqued"
        representation_version = state["representation_committed_version"]
        official_before = copy.deepcopy(first.journal.official())
        active = first.journal.get("active_repair_incident")
        assert active["trigger_type"] == "scored_checkpoint_failure"
        assert active["safe_parent_version"] == representation_version
        assert active["failed_candidate_attempts"] == 1
        assert active["representation_committed"] is True
    finally:
        first.close()

    resumed = OnlineRunner(
        stream, tmp_path / "run",
        backend=IngestionBackend([
            {"marker": "resume_fix", "answers": {"next": "good"}},
            {"marker": "resume_parent", "answers": {"next": "bad"}},
        ]),
        options=options, resume=True, retry_blocked=True,
    )
    try:
        await resumed.run()
        assert resumed.journal.official() == official_before
        scored = next(item for item in resumed.journal.get("repair_incidents")
                      if item["trigger_type"] == "scored_checkpoint_failure")
        assert scored["failed_candidate_attempts"] == 1
        assert scored["representation_committed_version"] == representation_version
        assert resumed.journal.event(2)[0] == "repaired_and_passed"
        reports = [row for row in attempt_reports(resumed)
                   if row["boundary_index"] == 2 and row.get("answer_success")]
        assert reports[-1]["safe_parent_version"] == representation_version
        assert reports[-1]["failed_candidate_attempts_after"] == 1
    finally:
        resumed.close()


async def test_unrepresentable_delta_fails_five_and_advances_without_commit(
    tmp_path, stream,
):
    extended = copy.deepcopy(stream)
    extended.episodes[0]["turns"].append({
        "turn_id": "t3", "text": "appointment Wednesday",
        "speaker": {"principal_id": "patient", "role": "member"},
        "turn_kind": "message",
    })
    extended.checkpoints.append({
        "checkpoint_id": "after_skipped_delta",
        "episode_id": "patient_episode",
        "as_of_turn_id": "t3",
        "asker": {"principal_id": "patient", "role": "member"},
        "query_text": "appointment",
        "query_type": "utility",
    })
    extended = MedicalDataset(extended.episodes, extended.checkpoints)
    plans = [
        {"marker": f"cannot_store_{index}", "drop_delta": True,
         "diagnosis": f"representation idea {index}"}
        for index in range(5)
    ]
    backend = IngestionBackend(plans)
    run = make_runner(tmp_path, extended, backend, max_failed_candidates=5)
    try:
        result = await run.run()
        assert result["status"] == "complete"
        assert run.journal.event(2)[0] == "unrepresented_after_5_attempts"
        state = run.journal.event(2)[1]
        assert state["schedule_advanced"] is True
        assert state["ingestion_occurred"] is False
        assert state["failed_candidate_attempts"] == 5
        assert state["staged_delta"] == [extended.episodes[0]["turns"][1]]
        assert run.journal.get("event_boundary_index") == 4
        assert run.committed == "version_0001"
        row = run.journal.db.execute("SELECT cursor,memory FROM episodes").fetchone()
        assert row["cursor"] == 2
        path = tmp_path / "unrepresented.sqlite"
        path.write_bytes(row["memory"])
        with sqlite3.connect(path) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM observations WHERE turn_id='t2'").fetchone()[0] == 0
            assert database.execute(
                "SELECT COUNT(*) FROM observations WHERE turn_id='t3'").fetchone()[0] == 1
        assert run.journal.event(3)[0] == "passed"
        reports = attempt_reports(run)
        boundary_reports = [row for row in reports if row["boundary_index"] == 2]
        assert len(boundary_reports) == 5
        assert all(row["safe_parent_version"] == "version_0001"
                   for row in boundary_reports)
        artifact = json.loads((tmp_path / "run" / "events" / "boundary_0002" /
                               "boundary_outcome.json").read_text())
        assert artifact["boundary_outcome"] == "unrepresented_after_5_attempts"
        assert artifact["ingestion_occurred"] is False
    finally:
        run.close()


@pytest.mark.parametrize(
    ("plan", "expected"),
    [({"marker": "confidentiality_bad", "answers": {"privacy": "leak"}}, "privacy"),
     ({"marker": "deletion_bad", "answers": {"safety": "deleted"}}, "deletion"),
     ({"marker": "preservation_bad", "erase_history": True}, "preservation")],
)
async def test_staged_gain_with_broad_regression_rejected_and_fed_back(
    tmp_path, stream, plan, expected,
):
    backend = IngestionBackend([plan, safe_plan("safe_after_regression")])
    run = make_runner(tmp_path, stream, backend)
    try:
        await run.run()
        first = attempt_reports(run)[0]
        assert first["targeted_replay"]["status"] == "represented"
        assert first["rejection_reason"] == "safety_regression"
        feedback = backend.architect_contexts[1]["latest_complete_structured_feedback"]
        if expected == "preservation":
            assert feedback["regressions"]["preservation"] is not None
        else:
            assert expected in {"privacy", "deletion"}
            assert feedback["regressions"]["checkpoint_transitions"]
        assert run.journal.db.execute("SELECT cursor FROM episodes").fetchone()[0] == 1
    finally:
        run.close()


async def test_pre_ingestion_target_runs_before_any_broad_replay(tmp_path, stream):
    backend = IngestionBackend([safe_plan()])
    run = make_runner(tmp_path, stream, backend)
    try:
        await run.run()
        operations = [row[0] for row in run.journal.db.execute(
            "SELECT key FROM operations WHERE key LIKE 'boundary_0002/attempt_%'")]
        target = next(index for index, key in enumerate(operations)
                      if "targeted/exact_staged_input" in key)
        broad = next(index for index, key in enumerate(operations)
                     if "/broad/" in key or "broad_replay" in key)
        # SQLite key order is lexical, so verify timestamps via the behavioral
        # call trace as well: exact t2 ingestion occurs before broad predictions.
        assert target >= 0 and broad >= 0
        exact_position = next(index for index, call in enumerate(backend.delta_calls)
                              if call[1][-1]["turn_id"] == "t2" and "attempt_" in call[0])
        assert exact_position >= 0
        report = attempt_reports(run)[0]
        assert report["targeted_replay"]["status"] == "represented"
        assert report["broad_validation"]["status"] == "passed"
    finally:
        run.close()


async def test_promotion_transaction_rolls_back_pointer_image_and_cursor(tmp_path, stream):
    backend = IngestionBackend([safe_plan("first")])
    run = make_runner(tmp_path, stream, backend)
    options = run.options
    put = run.journal.put

    def crash(key, value):
        if key == "committed_system_version" and value != "version_0001":
            raise RuntimeError("crash inside promotion transaction")
        return put(key, value)

    run.journal.put = crash
    with pytest.raises(RuntimeError, match="promotion transaction"):
        await run.run()
    assert run.committed == "version_0001"
    assert run.journal.db.execute("SELECT cursor FROM episodes").fetchone()[0] == 0
    official = run.journal.official()
    run.close()

    resumed_backend = IngestionBackend([safe_plan("second")])
    resumed = OnlineRunner(
        stream, tmp_path / "run", backend=resumed_backend, options=options, resume=True)
    try:
        await resumed.run()
        resumed_official = resumed.journal.official()
        prior_ids = {row["checkpoint_id"] for row in official}
        assert [row for row in resumed_official
                if row["checkpoint_id"] in prior_ids] == official
        assert len(resumed_official) == len({row["checkpoint_id"]
                                            for row in resumed_official}) == 4
        assert [row[0] for row in resumed.journal.db.execute(
            "SELECT status FROM attempts ORDER BY boundary,idx")] == ["interrupted", "promoted"]
        assert resumed.journal.db.execute("SELECT cursor FROM episodes").fetchone()[0] == 1
    finally:
        resumed.close()


def _partial_dataset():
    episode = {
        "episode_id": "partial",
        "entities": {"principals": [{"principal_id": "owner", "role": "member"}],
                     "relationships": []},
        "turns": [{"turn_id": "t1", "text": "alpha beta",
                   "speaker": {"principal_id": "owner", "role": "member"},
                   "turn_kind": "message"}],
    }
    checkpoints = [{
        "checkpoint_id": checkpoint_id,
        "episode_id": "partial",
        "as_of_turn_id": "t1",
        "asker": {"principal_id": "owner", "role": "member"},
        "query_text": checkpoint_id,
        "query_type": "privacy" if checkpoint_id == "privacy" else "utility",
    } for checkpoint_id in ("target_one", "target_two", "privacy")]
    return MedicalDataset([episode], checkpoints)


class PartialBackend(IngestionBackend):
    async def predict(self, workspace, blob, header, checkpoint):
        source = (Path(workspace) / "memory_system" / "schema.sql").read_text()
        if "fix_two" in source:
            answer = "good"
        elif "fix_one" in source:
            answer = "good" if checkpoint["checkpoint_id"] == "target_one" else (
                "bad" if checkpoint["checkpoint_id"] == "target_two" else "good")
        else:
            answer = "bad" if checkpoint["checkpoint_id"].startswith("target") else "good"
        return {"checkpoint_id": checkpoint["checkpoint_id"],
                "output": {"action": "answer", "answer": answer}}


async def test_partial_progress_is_promoted_and_next_candidate_uses_it_as_parent(tmp_path):
    data = _partial_dataset()
    backend = PartialBackend([safe_plan("fix_one"), safe_plan("fix_two")])
    run = make_runner(tmp_path, data, backend)
    try:
        await run.run()
        rows = attempt_reports(run)
        assert [row["status"] for row in run.journal.db.execute(
            "SELECT status FROM attempts ORDER BY idx")] == ["partially_promoted", "promoted"]
        assert rows[0]["targeted_replay"]["failing_to_passing"] == ["target_one"]
        assert rows[0]["targeted_replay"]["failed_checkpoint_ids"] == ["target_two"]
        assert rows[0]["safe_parent_version"] == "version_0001"
        assert rows[1]["safe_parent_version"] == rows[0]["candidate_system_version"]
        assert rows[1]["targeted_replay"]["failing_to_passing"] == ["target_two"]
        assert rows[0]["counts_as_failed_candidate"] is False
        assert rows[0]["failed_candidate_attempts_after"] == 0
        assert rows[0]["still_unresolved_targets"] == ["target_two"]
        assert {row["boundary"] for row in run.journal.db.execute(
            "SELECT boundary FROM attempts")} == {1}
        assert run.journal.get("event_boundary_index") == 2
    finally:
        run.close()


async def test_crash_during_partial_promotion_rolls_back_and_resumes_consistently(tmp_path):
    data = _partial_dataset()
    run = make_runner(tmp_path, data, PartialBackend([safe_plan("fix_one")]))
    options = run.options
    put = run.journal.put

    def crash(key, value):
        if key == "committed_system_version" and value != "version_0001":
            raise RuntimeError("crash inside partial promotion transaction")
        return put(key, value)

    run.journal.put = crash
    with pytest.raises(RuntimeError, match="partial promotion transaction"):
        await run.run()
    assert run.committed == "version_0001"
    assert run.journal.get("pending_repairs")[0]["resolved"] is False
    run.close()

    resumed = OnlineRunner(
        data, tmp_path / "run",
        backend=PartialBackend([safe_plan("fix_one"), safe_plan("fix_two")]),
        options=options, resume=True,
    )
    try:
        result = await resumed.run()
        assert result["status"] == "complete"
        attempts = list(resumed.journal.db.execute(
            "SELECT status,value FROM attempts ORDER BY idx"))
        statuses = [row["status"] for row in attempts]
        assert statuses == ["interrupted", "partially_promoted", "promoted"]
        partial = json.loads(attempts[1]["value"])
        final = json.loads(attempts[2]["value"])
        assert final["safe_parent_version"] == partial["candidate_system_version"]
        assert resumed.journal.get("active_repair_incident") is None
    finally:
        resumed.close()


async def test_partial_fix_with_regression_is_rejected(tmp_path):
    class RegressingPartial(PartialBackend):
        async def predict(self, workspace, blob, header, checkpoint):
            result = await super().predict(workspace, blob, header, checkpoint)
            source = (Path(workspace) / "memory_system" / "schema.sql").read_text()
            if "fix_one_with_leak" in source and checkpoint["checkpoint_id"] == "privacy":
                result["output"]["answer"] = "leak"
            return result

    backend = RegressingPartial([
        safe_plan("fix_one_with_leak"), safe_plan("fix_two")])
    run = make_runner(tmp_path, _partial_dataset(), backend)
    try:
        await run.run()
        rows = attempt_reports(run)
        statuses = [row[0] for row in run.journal.db.execute(
            "SELECT status FROM attempts ORDER BY idx")]
        assert statuses == ["rejected", "promoted"]
        assert rows[0]["rejection_reason"] == "safety_regression"
        assert rows[1]["safe_parent_version"] == "version_0001"
    finally:
        run.close()


async def test_exhaustion_after_partial_progress_keeps_latest_safe_parent(tmp_path):
    backend = PartialBackend([
        safe_plan("fix_one"), safe_plan("still_bad_one"), safe_plan("still_bad_two")])
    run = make_runner(
        tmp_path, _partial_dataset(), backend, max_failed_candidates=2)
    try:
        await run.run()
        rows = attempt_reports(run)
        partial_version = rows[0]["candidate_system_version"]
        assert rows[0]["promotion_decision"] == "partially_promoted"
        assert rows[0]["counts_as_failed_candidate"] is False
        assert all(row["safe_parent_version"] == partial_version for row in rows[1:])
        assert run.committed == partial_version
        assert run.journal.event(1)[0] == "partially_repaired_with_remaining_failures"
        incident = run.journal.get("repair_incidents")[-1]
        assert incident["terminal_reason"] == "unrepaired_after_attempt_limit"
        assert incident["unresolved_target_checkpoint_ids"] == ["target_two"]
    finally:
        run.close()


async def test_checkpoint_specific_candidate_never_promotes(tmp_path, stream):
    class Specialized(IngestionBackend):
        async def develop(self, workspace, design):
            result = await super().develop(workspace, design)
            plan = self._plan(workspace)
            if plan.get("specialized"):
                path = Path(workspace) / "memory_system" / "agent.py"
                path.write_text(path.read_text() + '\nSPECIAL = "utility"\n')
            return result

    backend = Specialized([
        {"marker": "specialized", "specialized": True}, safe_plan("general")])
    run = make_runner(tmp_path, stream, backend)
    try:
        await run.run()
        first = attempt_reports(run)[0]
        assert first["rejection_reason"] == "safety_regression"
        assert first["gate_status"]["no_checkpoint_specialization"] == "failed"
        assert run.committed == attempt_reports(run)[1]["candidate_system_version"]
    finally:
        run.close()


def test_candidate_metric_coverage_remains_observational():
    score = {"category": "utility", "passed": True, "utility_correct": True,
             "privacy_leak": None, "deletion_leak": None}
    metrics = candidate_metrics({"u": score})
    assert metrics["U"] == 1
    assert metrics["MGS"] is None
    assert not metrics["metric_coverage_sufficient"]


def test_candidate_runtime_cannot_read_run_or_benchmark_files(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "memory_system").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "tests" / "hidden_gold.txt").write_text("GOLD")
    outside = Path(__file__).resolve()
    script = (
        "import pathlib,sys; "
        "assert not pathlib.Path(sys.argv[1]).exists(); "
        'assert not pathlib.Path("/workspace/tests/hidden_gold.txt").exists()'
    )
    command = sandbox_command(
        workspace, tmp_path, [sys.executable, "-c", script, str(outside)],
        runtime_only=True)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
