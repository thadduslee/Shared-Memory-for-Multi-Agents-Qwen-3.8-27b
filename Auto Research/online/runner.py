"""Durable, blocking online adaptation with immutable official predictions.

Ingestion commits an episode image and cursor together. Promotion commits
migrated images, the version pointer, and any staged ingestion together. A
repair incident owns its boundary until every required target is resolved.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

import config
from gatemem_adapter import assert_no_hidden_fields, strip_hidden_fields
from online.backend import Backend, DeveloperApproachUnworkable, InfrastructureFailure
from online.diagnostics import build_incident_dossier, replay_behavior
from online.metrics import candidate_metrics, cumulative, promotion_regressions
from online.schedule import (
    POLICY,
    build_schedule,
    compatibility,
    episode_header,
    structural_summary,
    visible_checkpoint,
)
from online.storage import Journal, digest, encode, write_json
from online.validation import (
    ValidationFailure,
    build_gates,
    code_manifest,
    copy_workspace,
    dump,
    image_signature,
    migrate_blob,
    preserve,
    reject_checkpoint_specialization,
)

log = logging.getLogger("orchestrator.online")


@dataclass(frozen=True)
class OnlineOptions:
    """Online policy controls; none of these limits repair work or spend."""

    disable_redesign: bool = False
    replay_window: int = config.ONLINE_REPLAY_WINDOW
    utility_tolerance: float = config.ONLINE_UTILITY_TOLERANCE
    stream_order: str = POLICY
    max_infra_retries: int = config.MAX_INFRA_RETRIES
    max_stalled_attempts: int = config.ONLINE_MAX_STALLED_ATTEMPTS
    max_duplicate_proposals: int = config.ONLINE_MAX_DUPLICATE_PROPOSALS
    max_correction_rounds: int = config.ONLINE_MAX_CORRECTION_ROUNDS
    # Opt-in: rebuild replay and promotion images from the episode's turns under
    # each side's code, so ingestion-time repairs become measurable.
    replay_reingest: bool = False
    # Opt-in: predict and judge each replay side this many times; pass/fail is
    # the majority, so one noisy judge call cannot make or break a candidate.
    replay_votes: int = 1

    def __post_init__(self):
        if self.replay_window < 1 or self.max_infra_retries < 0:
            raise ValueError("invalid online replay/transport policy")
        if min(self.max_stalled_attempts, self.max_duplicate_proposals,
               self.max_correction_rounds) < 1:
            raise ValueError("online stall limits must be at least one")
        if self.replay_votes < 1 or self.replay_votes % 2 == 0:
            raise ValueError("replay_votes must be a positive odd number")
        if not 0 <= self.utility_tolerance <= 1:
            raise ValueError("online utility tolerance must be between zero and one")


class BudgetExhausted(RuntimeError):
    """Legacy import compatibility; online repair no longer raises this."""


class RepairBlocked(RuntimeError):
    pass


# Rejections that repeat earlier work count toward the duplicate limit too.
_DUPLICATE_REASONS = {
    "equivalent_previous_attempt", "duplicate_source_change", "duplicate_replay_behavior",
}


_LEGACY_LIMIT_KEYS = {
    "max_repair_attempts",
    "max_pre_ingestion_attempts",
    "max_post_evaluation_attempts",
    "repair_min_scored_checkpoints",
    "repair_min_category_coverage",
    "max_developer_turns_per_candidate",
    "max_candidate_tokens",
    "max_repair_generation_tokens",
    "max_tokens",
    "max_wallclock_s",
    "promotion_min_mgs",
    "promotion_max_a",
    "promotion_max_f",
    "min_category_coverage",
}


class OnlineRunner:
    def __init__(
        self,
        dataset,
        run_dir,
        *,
        backend=None,
        options=None,
        resume=False,
        retry_blocked=False,
        abandon_blocked=False,
        checkpointer_path=None,
    ):
        self.dataset = dataset  # scoring side only; never passed wholesale to an agent
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = Path(checkpointer_path or self.run_dir / "checkpoints.sqlite").resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("online run is already owned by another process") from None
        self.journal = Journal(self.path)
        self.backend = backend or Backend(config.MOCK_MODE)
        self.options = options or OnlineOptions()
        self.retry_blocked = retry_blocked
        self.abandon_blocked = abandon_blocked
        self.schedule = build_schedule(dataset, self.options.stream_order)
        self.checkpoints = {row["checkpoint_id"]: row for row in dataset.checkpoints}
        fingerprint = digest({
            "episodes": dataset.episodes,
            "checkpoints": sorted(dataset.checkpoints, key=lambda row: row["checkpoint_id"]),
        })
        existing = self.journal.get("manifest")
        try:
            if existing:
                if not resume:
                    raise ValueError("run exists; use --resume")
                if (existing["dataset_digest"] != fingerprint or
                        not self._resume_options_compatible(
                            existing.get("options", {}), asdict(self.options))):
                    raise ValueError("resume dataset or causal configuration mismatch")
                if existing["mock"] != self.backend.mock or existing["run_dir"] != str(self.run_dir):
                    raise ValueError("resume backend or artifact directory mismatch")
                legacy = {key: value for key, value in existing.get("options", {}).items()
                          if key in _LEGACY_LIMIT_KEYS}
                if legacy or existing.get("options") != asdict(self.options):
                    existing = {
                        **existing,
                        "format_version": max(2, int(existing.get("format_version", 1))),
                        "options": asdict(self.options),
                        "legacy_online_limits_ignored": legacy,
                    }
                    with self.journal.db:
                        self.journal.put("manifest", existing)
            elif resume:
                raise ValueError("no durable run to resume")
            else:
                self._initialize(fingerprint)
        except Exception:
            self.close()
            raise
        self.current_operation = ""
        self.backend.charge = self._charge
        if resume:
            unfinished = self.journal.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE status='building'").fetchone()[0]
            if unfinished:
                self._interrupt_unfinished_candidates()
                active = self.journal.get("active_repair_incident")
                if active:
                    self._transition(
                        active, "architect", candidate_system_version=None,
                        resume_note="interrupted candidate marked non-promoted; incident resumes",
                    )
        self.export()

    @staticmethod
    def _resume_options_compatible(stored, requested):
        current = {key: value for key, value in stored.items()
                   if key not in _LEGACY_LIMIT_KEYS}
        return all(key not in current or current[key] == value
                   for key, value in requested.items())

    @property
    def committed(self):
        return self.journal.get("committed_system_version")

    def workspace(self, version=None):
        return self.run_dir / "versions" / (version or self.committed) / "workspace"

    def version_manifest(self, version=None):
        return self.journal.get("versions")[version or self.committed]

    def _initialize(self, fingerprint):
        version = "version_0001"
        work = self.workspace(version)
        copy_workspace(config.TEMPLATES_DIR, work)
        manifest = {
            "schema_version": 1,
            "code": code_manifest(work),
            "parent": None,
            "migration_sql": "",
            "status": "committed",
            "promoted_after_boundary": 0,
            "supported_turn_fields": ["turn_id", "timestamp", "speaker", "turn_kind", "text"],
        }
        run_manifest = {
            "format_version": 2,
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir),
            "dataset_digest": fingerprint,
            "options": asdict(self.options),
            "ordering_policy": POLICY,
            "episode_turn_order": "recorded turns array order",
            "global_order_assumption": "No authoritative inter-episode chronology; lexical episode ID order.",
            "entity_assumption": "Episode entities are static initial metadata.",
            "mock": self.backend.mock,
            "scorer": "GateMem rules" if self.backend.mock else "GateMem rules + configured LLM Judge",
            "started_at": time.time(),
            "checkpointer_path": str(self.path),
        }
        with self.journal.db:
            self.journal.put("manifest", run_manifest)
            self.journal.put("committed_system_version", version)
            self.journal.put("candidate_system_version", None)
            self.journal.put("event_boundary_index", 1)
            self.journal.put("repair_attempt_index", 0)
            self.journal.put("incident_sequence", 0)
            self.journal.put("active_repair_incident", None)
            self.journal.put("repair_incidents", [])
            self.journal.put("versions", {version: manifest})
            self.journal.put("status", "ready")
            self.journal.put("cumulative_counts", cumulative([]))
            self.journal.put("pending_repairs", [])
        write_json(work.parent / "manifest.json", manifest)
        write_json(work.parent / "parent.json", {"parent": None})
        write_json(work.parent / "promotion_report.json", {"baseline": True})
        write_json(self.run_dir / "event_schedule.json", {"policy": POLICY, "events": self.schedule})
        write_json(self.run_dir / "run_manifest.json", run_manifest)

    # Observable operations. Tokens and time are never stop conditions.
    def _charge(self, usage):
        tokens = int((usage or {}).get("total_tokens") or 0)
        with self.journal.db:
            self.journal.db.execute(
                "INSERT OR IGNORE INTO operations(key) VALUES (?)", (self.current_operation,))
            self.journal.db.execute(
                "UPDATE operations SET tokens=tokens+? WHERE key=?",
                (tokens, self.current_operation),
            )

    def _usage_for_prefix(self, prefix):
        row = self.journal.db.execute(
            "SELECT COALESCE(SUM(tokens),0),COALESCE(SUM(seconds),0) "
            "FROM operations WHERE key LIKE ?", (prefix + "%",),
        ).fetchone()
        return {"tokens": int(row[0]), "seconds": float(row[1])}

    def _attempt_usage(self, index, attempt_idx):
        return self._usage_for_prefix(f"boundary_{index:04d}/attempt_{attempt_idx:02d}/")

    async def operation(self, key, call):
        self.current_operation = key
        while True:
            started = time.monotonic()
            with self.journal.db:
                self.journal.db.execute("INSERT OR IGNORE INTO operations(key) VALUES (?)", (key,))
            try:
                return await call()
            except (InfrastructureFailure, ConnectionError, TimeoutError) as exc:
                with self.journal.db:
                    self.journal.db.execute(
                        "UPDATE operations SET retries=retries+1 WHERE key=?", (key,))
                retries = self.journal.db.execute(
                    "SELECT retries FROM operations WHERE key=?", (key,)).fetchone()[0]
                if retries > self.options.max_infra_retries:
                    raise InfrastructureFailure(
                        f"{key}: transport retry limit reached; boundary remains blocked") from exc
                log.warning("Retrying same online operation %s after transport failure (%d)",
                            key, retries)
                await asyncio.sleep(min(0.1 * 2 ** (retries - 1), 2))
            finally:
                with self.journal.db:
                    self.journal.db.execute(
                        "UPDATE operations SET seconds=seconds+? WHERE key=?",
                        (time.monotonic() - started, key),
                    )

    # Visible causal context and durable incidents.
    def _visible_outcome(self, row):
        checkpoint = visible_checkpoint(self.checkpoints[row["checkpoint_id"]])
        output = row["prediction"].get("output", {})
        return {
            "checkpoint_id": row["checkpoint_id"],
            "event_boundary_index": row["event_boundary_index"],
            "query": checkpoint,
            "passed": row["score"]["passed"],
            "observed_action": output.get("action"),
        }

    def _context(self, index, summary, *, post=False, error=None):
        prior = [row for row in self.journal.official()
                 if row.get("score") and row["event_boundary_index"] < index + int(post)]
        result = {
            "event_boundary_index": index,
            "structural_summary": summary,
            "committed_system_version": self.committed,
            "manifest": self.version_manifest(),
            "episode_cursors": {row["id"]: row["cursor"]
                                for row in self.journal.db.execute("SELECT id,cursor FROM episodes")},
            "earlier_outcomes" if not post else "observed_outcomes": [
                self._visible_outcome(row) for row in prior[-self.options.replay_window:]
            ],
            "cumulative_metrics": cumulative(prior),
            "ingestion_error": error,
        }
        assert_no_hidden_fields(result, where="online causal context")
        return result

    def _record_pending_failures(self, rows):
        pending = list(self.journal.get("pending_repairs", []))
        known = {item["checkpoint_id"] for item in pending}
        for row in rows:
            if row.get("score", {}).get("passed") or row["checkpoint_id"] in known:
                continue
            pending.append({
                "checkpoint_id": row["checkpoint_id"],
                "event_boundary_index": row["event_boundary_index"],
                "first_observed_at_boundary": row["event_boundary_index"],
                "resolved": False,
            })
        with self.journal.db:
            self.journal.put("pending_repairs", pending)
        return pending

    def _save_incident(self, incident):
        assert_no_hidden_fields(incident, where="durable repair incident")
        history = list(self.journal.get("repair_incidents", []))
        for position, previous in enumerate(history):
            if previous.get("incident_id") == incident["incident_id"]:
                history[position] = incident
                break
        else:
            history.append(incident)
        with self.journal.db:
            self.journal.put("active_repair_incident", None if incident.get("resolved") else incident)
            self.journal.put("unresolved_repair_incident", None if incident.get("resolved") else incident)
            self.journal.put("repair_incidents", history)
            if not incident.get("resolved") and incident.get("phase") != "infrastructure_blocked":
                self.journal.put("status", "repairing")
        folder = self.run_dir / "events" / f"boundary_{incident['boundary_index']:04d}"
        write_json(folder / "repair_incident.json", incident)
        if incident.get("architect_incident_dossier"):
            write_json(folder / "architect_incident_dossier.json",
                       incident["architect_incident_dossier"])

    def _transition(self, incident, phase, **updates):
        changed = {**incident, **updates, "phase": phase, "updated_at": time.time()}
        self._save_incident(changed)
        log.info(
            "repair incident %s boundary=%d phase=%s unresolved=%d parent=%s",
            changed["incident_id"], changed["boundary_index"], phase,
            len(changed.get("unresolved_target_checkpoint_ids") or []),
            changed["safe_parent_version"],
        )
        return changed

    def _interrupt_unfinished_candidates(self):
        rows = list(self.journal.db.execute(
            "SELECT boundary,idx,value FROM attempts WHERE status='building' ORDER BY boundary,idx"))
        for row in rows:
            report = json.loads(row["value"])
            report.update({
                "failed_gate": "interrupted",
                "rejection_reason": "interrupted_candidate",
                "reasons": ["interrupted candidate was never promoted"],
                "phase": "rejected_with_feedback",
            })
            self._finish_attempt(row["boundary"], row["idx"], "interrupted", report)
        if rows:
            with self.journal.db:
                self.journal.put("candidate_system_version", None)

    def _make_incident_dossier(self, incident_id, index, trigger_type, targets,
                               summary, ingestion_error):
        if trigger_type == "scored_checkpoint_failure":
            target_rows = [row for row in self.journal.official()
                           if row["checkpoint_id"] in set(targets)]
            return build_incident_dossier(
                incident_id, index, target_rows, self.checkpoints)
        trace_id = f"{incident_id}:staged_input"
        dossier = {
            "format_version": 1,
            "incident_id": incident_id,
            "boundary_index": index,
            "causal_visibility": "boundary_visible_only",
            "targets": [{
                "trace_id": trace_id,
                "decision_stage": "pre_ingestion_validation",
                "decision_rule": "compatibility_or_ingestion_validation",
                "outcome": "rejected",
                "structural_summary": summary,
                "error": ingestion_error,
            }],
            "trace_entry_ids": [trace_id],
        }
        assert_no_hidden_fields(dossier, where="pre-ingestion architect dossier")
        return dossier

    def _ensure_incident(
        self,
        index,
        *,
        trigger_type,
        summary,
        target_ids=None,
        staged_input=None,
        ingestion_error=None,
    ):
        active = self.journal.get("active_repair_incident")
        if active and not active.get("resolved"):
            if active["boundary_index"] != index:
                raise RepairBlocked(
                    f"active repair incident {active['incident_id']} owns boundary "
                    f"{active['boundary_index']}; refusing to advance to {index}")
            if staged_input is not None and active.get("staged_input_digest") != digest(staged_input):
                raise ValidationFailure("staged input changed while repair incident was active")
            if not active.get("architect_incident_dossier"):
                active = {
                    **active,
                    "architect_incident_dossier": self._make_incident_dossier(
                        active["incident_id"], index, active["trigger_type"],
                        active.get("unresolved_target_checkpoint_ids") or [],
                        active.get("structural_summary"), active.get("ingestion_error")),
                    "diagnosis_review_history": active.get("diagnosis_review_history") or [],
                }
                self._save_incident(active)
            return active

        self._interrupt_unfinished_candidates()
        sequence = int(self.journal.get("incident_sequence", 0)) + 1
        targets = sorted(set(target_ids or []))
        parent = self.committed
        prior_attempts = []
        for row in self.journal.db.execute(
                "SELECT boundary,idx,status,value FROM attempts ORDER BY boundary,idx"):
            report = json.loads(row["value"])
            if report.get("safe_parent_version") != parent:
                continue
            expected = ("post_evaluation" if trigger_type == "scored_checkpoint_failure"
                        else "pre_ingestion")
            if report.get("trigger_type") not in {trigger_type, expected}:
                continue
            prior_attempts.append({
                "boundary_index": row["boundary"], "attempt_index": row["idx"],
                "status": row["status"], "diagnosis_signature": report.get("diagnosis_signature"),
                "design_signature": report.get("design_signature"),
                "migration_signature": report.get("migration_signature"),
                "source_diff_signature": report.get("source_diff_signature"),
                "behavioral_signature": report.get("behavioral_signature"),
                "rejection_reason": report.get("rejection_reason") or report.get("failed_gate"),
            })
        incident_id = f"incident_{sequence:04d}"
        dossier = self._make_incident_dossier(
            incident_id, index, trigger_type, targets, summary, ingestion_error)
        incident = {
            "incident_id": incident_id,
            "boundary_index": index,
            "trigger_type": trigger_type,
            "repair_objective": (
                "represent and atomically ingest the exact staged input"
                if trigger_type == "pre_ingestion" else
                "make at least one unresolved failing checkpoint pass without regression"
            ),
            "safe_parent_version": parent,
            "target_checkpoint_ids": targets,
            "unresolved_target_checkpoint_ids": targets,
            "staged_input": staged_input,
            "staged_input_digest": digest(staged_input) if staged_input is not None else None,
            "ingestion_error": ingestion_error,
            "structural_summary": summary,
            "candidate_attempt_history": prior_attempts,
            "structured_feedback_history": [],
            "diagnosis_review_history": [],
            "architect_incident_dossier": dossier,
            "promotion_history": [],
            "phase": "architect",
            "resolved": False,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self.journal.db:
            self.journal.put("incident_sequence", sequence)
        self._save_incident(incident)
        return incident

    def _architect_context(self, incident):
        targets = set(incident.get("unresolved_target_checkpoint_ids") or [])
        official = [row for row in self.journal.official()
                    if row.get("score") and row["event_boundary_index"] <= incident["boundary_index"]]
        feedback_history = incident.get("structured_feedback_history") or []
        previous = feedback_history[-1:]
        earlier = feedback_history[:-1]
        context = {
            "incident_report": {
                "incident_id": incident["incident_id"],
                "boundary_index": incident["boundary_index"],
                "trigger_type": incident["trigger_type"],
                "repair_objective": incident["repair_objective"],
                "safe_parent_version": incident["safe_parent_version"],
                "structural_summary": incident.get("structural_summary"),
                "ingestion_error": incident.get("ingestion_error"),
                "exact_staged_input": incident.get("staged_input"),
                "exact_staged_input_digest": incident.get("staged_input_digest"),
                "unresolved_targets": [self._visible_outcome(row) for row in official
                                       if row["checkpoint_id"] in targets],
                "previous_approaches": incident.get("candidate_attempt_history", []),
            },
            "architect_incident_dossier": incident.get("architect_incident_dossier"),
            "latest_complete_structured_feedback": previous[0] if previous else None,
            "earlier_attempt_summaries": [self._compact_feedback(item) for item in earlier],
            "previous_rejected_candidate": previous[0] if previous else None,
        }
        assert_no_hidden_fields(context, where="online Architect incident context")
        return context

    @staticmethod
    def _compact_feedback(feedback):
        return {key: feedback.get(key) for key in (
            "triggering_boundary", "safe_parent_version", "diagnosis_attempted",
            "focused_goal_attempted", "work_order_attempted", "rejection_reason",
            "failure_classification", "diagnosis_signature", "design_signature",
            "source_diff_signature", "behavioral_signature",
        )}

    # Main prequential state machine.
    async def run(self, max_boundaries=None):
        processed = 0
        try:
            status = self.journal.get("status")
            if status == "infrastructure_blocked":
                if not self.retry_blocked:
                    raise InfrastructureFailure(self.journal.get("halt_reason"))
                incident = self.journal.get("active_repair_incident")
                if incident:
                    incident = self._transition(
                        incident, "architect", infrastructure_error=None,
                        resume_note="operator requested retry of the same blocked boundary")
                with self.journal.db:
                    self.journal.put("status", "repairing" if incident else "ready")
                    self.journal.put("halt_reason", None)
                self.retry_blocked = False
            elif status in {"repair_blocked", "data_quarantined", "budget_exhausted"}:
                if not (self.retry_blocked or self.abandon_blocked):
                    raise RepairBlocked(self.journal.get("halt_reason"))
                incident = self.journal.get("active_repair_incident")
                if incident and self.abandon_blocked:
                    self._abandon_incident(incident)
                elif incident:
                    self._transition(
                        incident, "architect", stalled_rejections=0, duplicate_rejections=0,
                        resume_note="operator requested retry of the same stalled boundary")
                with self.journal.db:
                    self.journal.put("status", "ready")
                    self.journal.put("halt_reason", None)
                self.retry_blocked = self.abandon_blocked = False

            for event in self.schedule:
                index = event["event_boundary_index"]
                if index < self.journal.get("event_boundary_index"):
                    continue
                if max_boundaries is not None and processed >= max_boundaries:
                    break
                await self.boundary(event)
                processed += 1
                self.export()
                log.info("Online boundary %d/%d committed (%s)",
                         index, len(self.schedule), self.committed)
            with self.journal.db:
                self.journal.put(
                    "status",
                    "complete" if self.journal.get("event_boundary_index") > len(self.schedule)
                    else "paused",
                )
        except InfrastructureFailure as exc:
            incident = self.journal.get("active_repair_incident")
            if incident:
                self._transition(incident, "infrastructure_blocked", infrastructure_error=str(exc))
            with self.journal.db:
                self.journal.put("status", "infrastructure_blocked")
                self.journal.put("halt_reason", str(exc))
            raise
        finally:
            self.export()
        return self.summary()

    async def boundary(self, event):
        index, episode_id = event["event_boundary_index"], event["episode_id"]
        prefix = f"boundary_{index:04d}"
        phase, state, snapshot = self.journal.event(index)
        episode = self.dataset.episodes_by_id[episode_id]
        header = episode_header(episode)
        folder = self.run_dir / "events" / prefix
        folder.mkdir(parents=True, exist_ok=True)

        if phase == "new":
            row = self.journal.db.execute(
                "SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            cursor = row["cursor"] if row else -1
            if event["turn_position"] <= cursor:
                raise RuntimeError("episode cursor would move backwards")
            delta = strip_hidden_fields(episode["turns"][cursor + 1:event["turn_position"] + 1])
            summary = structural_summary(delta)
            check = compatibility(summary, self.version_manifest())
            write_json(folder / "input_delta.json", {
                "episode_id": episode_id, "previous_cursor": cursor, "turns": delta,
            })
            write_json(folder / "compatibility.json", check)
            state = {
                "compatibility": check,
                "summary": summary,
                "staged_delta": delta,
                "delta_digest": digest(delta),
                "ingested_turn_ids": [turn["turn_id"] for turn in delta],
            }
            with self.journal.db:
                self.journal.save_event(index, "staged", state)
            phase = "staged"

        if phase == "staged":
            row = self.journal.db.execute(
                "SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            cursor = row["cursor"] if row else -1
            fresh_delta = strip_hidden_fields(
                episode["turns"][cursor + 1:event["turn_position"] + 1])
            delta = state.get("staged_delta", fresh_delta)
            if (digest(delta) != digest(fresh_delta) or
                    state.get("delta_digest") not in {None, digest(delta)}):
                raise ValidationFailure("staged delta does not match durable dataset/cursor")
            check = state["compatibility"]
            version = self.committed
            self._verify_version(version)
            blob = row["memory"] if row else None
            if blob is None and version != "version_0001":
                blob, _ = await self.operation(
                    prefix + "/initialize_episode",
                    partial(self.backend.memory, self.workspace(version), None, header, delta=[]),
                )
                blob = self._migrate_to(blob, "version_0001", version)
            ingest_error = None
            try:
                snapshot, _ = await self.operation(
                    prefix + "/ingest",
                    partial(self.backend.memory, self.workspace(version), blob, header, delta=delta),
                )
                self._isolation(snapshot, episode_id, header)
                self._validate_delta_ingested(snapshot, episode_id, delta)
            except (ValidationFailure, sqlite3.Error) as exc:
                ingest_error = str(exc)
            if ingest_error or check["decision"] != "NO_CHANGE":
                incident = self._ensure_incident(
                    index,
                    trigger_type="pre_ingestion",
                    summary=state["summary"],
                    staged_input={
                        "episode_id": episode_id,
                        "previous_cursor": cursor,
                        "target_cursor": event["turn_position"],
                        "turns": delta,
                    },
                    ingestion_error=ingest_error or check["reason"],
                )
                if self.options.disable_redesign:
                    reason = f"online redesign disabled for active ingestion incident at boundary {index}"
                    with self.journal.db:
                        self.journal.put("status", "data_quarantined")
                        self.journal.put("halt_reason", reason)
                    raise RepairBlocked(reason)
                await self.repair_incident(incident, event=event, header=header)
                phase, state, snapshot = self.journal.event(index)
                if phase != "ingested":
                    raise RepairBlocked("staged ingestion incident did not commit")
            else:
                state = {**state, "frozen_version": version}
                with self.journal.db:
                    self.journal.db.execute(
                        "INSERT OR REPLACE INTO episodes VALUES (?, ?, ?)",
                        (episode_id, event["turn_position"], snapshot),
                    )
                    self.journal.save_event(index, "ingested", state, snapshot)
                phase = "ingested"

        write_json(folder / "frozen_version.json", {
            "committed_system_version": state["frozen_version"],
        })

        if phase == "ingested":
            self._verify_version(state["frozen_version"])
            for checkpoint_id in event["checkpoint_ids"]:
                if self.journal.db.execute(
                        "SELECT 1 FROM official WHERE id=?", (checkpoint_id,)).fetchone():
                    continue
                checkpoint = visible_checkpoint(self.checkpoints[checkpoint_id])
                prediction = await self.operation(
                    prefix + "/predict/" + checkpoint_id,
                    partial(
                        self.backend.predict,
                        self.workspace(state["frozen_version"]), snapshot, header, checkpoint,
                    ),
                )
                assert_no_hidden_fields(prediction, where="official evaluator prediction")
                with self.journal.db:
                    self.journal.db.execute(
                        "INSERT INTO official VALUES (?, ?, ?, ?, NULL)",
                        (checkpoint_id, index, state["frozen_version"], encode(prediction)),
                    )
            with self.journal.db:
                self.journal.save_event(index, "predicted", state, snapshot)
            phase = "predicted"

        if phase == "predicted":
            rows = [row for row in self.journal.official()
                    if row["event_boundary_index"] == index and not row["score"]]
            for row in rows:
                checkpoint_id = row["checkpoint_id"]
                score = await self.operation(
                    prefix + "/judge/" + checkpoint_id,
                    partial(self.backend.judge, row["prediction"], self.checkpoints[checkpoint_id]),
                )
                with self.journal.db:
                    self.journal.db.execute(
                        "UPDATE official SET score=? WHERE id=?", (encode(score), checkpoint_id))
            with self.journal.db:
                self.journal.save_event(index, "judged", state, snapshot)
                self.journal.put("cumulative_counts", cumulative(self.journal.official()))
            phase = "judged"

        if phase == "judged":
            rows = [row for row in self.journal.official()
                    if row["event_boundary_index"] == index]
            critique = await self.operation(prefix + "/critic", lambda: self.backend.critique(rows))
            state["critique"] = critique
            with self.journal.db:
                self.journal.save_event(index, "critiqued", state, snapshot)
            phase = "critiqued"

        if phase == "critiqued":
            current = [row for row in self.journal.official()
                       if row["event_boundary_index"] == index and row.get("score")]
            pending = self._record_pending_failures(current)
            unresolved = [item["checkpoint_id"] for item in pending
                          if not item.get("resolved") and not item.get("abandoned")]
            if unresolved:
                incident = self._ensure_incident(
                    index,
                    trigger_type="scored_checkpoint_failure",
                    summary=state["summary"],
                    target_ids=unresolved,
                )
                if self.options.disable_redesign:
                    reason = f"online redesign disabled for active scored incident at boundary {index}"
                    with self.journal.db:
                        self.journal.put("status", "repair_blocked")
                        self.journal.put("halt_reason", reason)
                    raise RepairBlocked(reason)
                await self.repair_incident(incident, event=event, header=header)
                unresolved = [item for item in self.journal.get("pending_repairs", [])
                              if not item.get("resolved") and not item.get("abandoned")]
                if unresolved:
                    raise RepairBlocked("active scored incident returned with unresolved targets")
                state["repair_disposition"] = "resolved"
            with self.journal.db:
                self.journal.save_event(index, "done", state, snapshot)
                self.journal.put("event_boundary_index", index + 1)
                self.journal.put("repair_attempt_index", 0)

    # Focused repair loop.
    def _next_attempt_index(self, boundary):
        row = self.journal.db.execute(
            "SELECT COALESCE(MAX(idx),0) FROM attempts WHERE boundary=?", (boundary,)).fetchone()
        return int(row[0]) + 1

    @staticmethod
    def _normalize(text):
        return " ".join(str(text or "").lower().split())

    @staticmethod
    def _validate_focused_design(design):
        if not isinstance(design, dict):
            raise ValidationFailure("Architect proposal is not an object", stage="architect")
        if design.get("decision") not in {"DATA_MIGRATION", "CODE_OR_SCHEMA_CHANGE"}:
            raise ValidationFailure(
                "Architect must propose one actionable repair for an unresolved incident",
                stage="architect",
            )
        diagnosis = str(design.get("diagnosis") or "").strip()
        goal = str(design.get("focused_goal") or "").strip()
        work_order = design.get("work_order")
        if not diagnosis or not goal or not isinstance(work_order, list) or len(work_order) != 1:
            raise ValidationFailure(
                "Architect proposal must contain one diagnosis, one focused_goal, and a one-item work_order",
                stage="architect",
            )
        if not str(work_order[0]).strip():
            raise ValidationFailure("Architect work order is empty", stage="architect")

    @staticmethod
    def _dossier_trace_rules(dossier):
        found = {}

        def walk(value):
            if isinstance(value, dict):
                trace_id = value.get("trace_id")
                if trace_id:
                    rule = str(value.get("decision_rule") or value.get("rule") or "")
                    if rule or str(trace_id) not in found:
                        found[str(trace_id)] = rule
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(dossier or {})
        return found

    def _diagnosis_review(self, incident, design, report, previous):
        """Read-only, deterministic proposal gate over the durable safe dossier."""
        checks = {}
        feedback = []
        trace_rules = self._dossier_trace_rules(incident.get("architect_incident_dossier"))
        citations = design.get("trace_citations")
        citations = citations if isinstance(citations, list) else []
        missing = sorted({str(item) for item in citations if str(item) not in trace_rules})
        checks["cited_traces_exist"] = bool(citations) and not missing
        if not citations:
            feedback.append("cite one or more trace_id values from architect_incident_dossier")
        elif missing:
            feedback.append("unknown trace citations: " + ", ".join(missing))

        addressed = str(design.get("addressed_decision_rule") or "").strip()
        cited_rules = {trace_rules.get(str(item), "") for item in citations}
        generic_rule_terms = {
            "record", "official", "output", "decision", "rule", "gate", "selected",
            "composition", "validation", "compatibility", "ingestion", "authorized",
            "evidence", "release", "coverage", "source", "group", "limit",
        }
        rule_terms = {
            token for token in addressed.lower().replace("-", "_").split("_")
            if len(token) > 4 and token not in generic_rule_terms
        }
        proposal_text = self._normalize(" ".join([
            str(design.get("diagnosis") or ""), str(design.get("focused_goal") or ""),
            " ".join(str(item) for item in (design.get("work_order") or [])),
        ]))
        rule_is_addressed = not rule_terms or any(term in proposal_text for term in rule_terms)
        checks["addresses_cited_decision_rule"] = bool(
            addressed and addressed in cited_rules and rule_is_addressed)
        if not checks["addresses_cited_decision_rule"]:
            feedback.append(
                "addressed_decision_rule must exactly name a cited rule and the diagnosis/work order "
                "must address its distinctive mechanism")

        prediction = design.get("predicted_replay_change")
        observable = prediction.get("observable") if isinstance(prediction, dict) else None
        expected = prediction.get("expected_change") if isinstance(prediction, dict) else None
        allowed_observables = {
            "action", "rendered_answer", "authorized_evidence", "decision_trace", "pass_fail",
        }
        checks["predicted_change_is_observable"] = (
            observable in allowed_observables and bool(str(expected or "").strip()))
        if not checks["predicted_change_is_observable"]:
            feedback.append(
                "predicted_replay_change needs an observable field and a concrete expected_change")

        duplicate = next((item for item in reversed(previous)
                          if item.get("db_attempt_index") != report["attempt_index"]
                          and item.get("diagnosis_signature") == report.get("diagnosis_signature")
                          and item.get("design_signature") == report.get("design_signature")
                          and item.get("migration_signature") == report.get("migration_signature")), None)
        checks["materially_different_from_rejected_attempts"] = duplicate is None
        if duplicate:
            feedback.append(
                f"proposal duplicates rejected attempt {duplicate['db_attempt_index']}")

        try:
            assert_no_hidden_fields(design, where="diagnosis review proposal")
            checks["uses_only_visible_information"] = True
        except AssertionError as exc:
            checks["uses_only_visible_information"] = False
            feedback.append(str(exc))

        accepted = all(checks.values())
        return {
            "status": "accepted" if accepted else "rejected",
            "checks": checks,
            "cited_trace_ids": [str(item) for item in citations],
            "cited_decision_rules": sorted(rule for rule in cited_rules if rule),
            "feedback": feedback,
            "duplicate_of_attempt": ({
                "boundary_index": duplicate["boundary_index"],
                "attempt_index": duplicate["db_attempt_index"],
            } if duplicate else None),
            "rejection_reason": (
                None if accepted else
                "equivalent_previous_attempt" if duplicate else "diagnosis_review_rejected"),
        }

    def _record_diagnosis_review(self, incident, review, attempt_idx):
        entry = {**review, "attempt_index": attempt_idx, "reviewed_at": time.time()}
        changed = {
            **incident,
            "diagnosis_review_history": list(
                incident.get("diagnosis_review_history") or []) + [entry],
            "latest_diagnosis_review": entry,
        }
        self._save_incident(changed)
        log.info(
            "repair incident %s phase=diagnosis_review attempt=%d result=%s reason=%s",
            incident["incident_id"], attempt_idx, review["status"],
            review.get("rejection_reason") or "accepted",
        )
        return changed

    def _prior_reports(self, incident, *, status=None):
        reports = []
        for row in self.journal.db.execute(
                "SELECT boundary,idx,status,value FROM attempts ORDER BY boundary,idx"):
            if status and row["status"] not in status:
                continue
            report = json.loads(row["value"])
            if report.get("safe_parent_version") != incident["safe_parent_version"]:
                continue
            old_type = report.get("trigger_type")
            expected = ("post_evaluation" if incident["trigger_type"] ==
                        "scored_checkpoint_failure" else "pre_ingestion")
            if old_type not in {incident["trigger_type"], expected}:
                continue
            reports.append({**report, "boundary_index": row["boundary"],
                            "db_attempt_index": row["idx"], "status": row["status"]})
        return reports

    async def repair_incident(self, incident, *, event, header):
        index = incident["boundary_index"]
        prefix = f"boundary_{index:04d}"
        while (incident.get("unresolved_target_checkpoint_ids") or
               incident["trigger_type"] == "pre_ingestion"):
            self._raise_if_stalled(incident)
            attempt_idx = self._next_attempt_index(index)
            candidate_number = len(incident.get("candidate_attempt_history") or []) + 1
            attempt_dir = self.run_dir / "events" / prefix / "attempts" / f"attempt_{attempt_idx:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            parent = incident["safe_parent_version"]
            report = {
                "incident_id": incident["incident_id"],
                "boundary_index": index,
                "trigger_type": incident["trigger_type"],
                "repair_objective": incident["repair_objective"],
                "attempt_index": attempt_idx,
                "candidate_number": candidate_number,
                "safe_parent_version": parent,
                "parent": parent,
                "candidate_system_version": None,
                "phase": "architect",
                "promotion_eligible": False,
                "reasons": [],
                "failed_checkpoint_ids": None,
                "gate_status": {name: "not_run" for name in (
                    "diagnosis_review", "developer", "compile", "invariants", "unit_tests", "migration",
                    "data_preservation", "database_integrity", "episode_isolation",
                    "patient_isolation", "no_checkpoint_specialization", "exact_staged_input",
                    "targeted_replay", "privacy", "deletion", "utility", "broad_validation",
                )},
                "targeted_replay": {"status": "not_run"},
                "broad_validation": {"status": "not_run"},
                "started_at": time.time(),
            }
            with self.journal.db:
                self.journal.db.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?)",
                    (index, attempt_idx, "building", encode(report)),
                )
                self.journal.put("repair_attempt_index", attempt_idx)
            incident = self._transition(
                incident, "architect", current_candidate_number=candidate_number,
                current_attempt_index=attempt_idx,
            )
            op = prefix + f"/attempt_{attempt_idx:02d}"
            try:
                raw_design = await self.operation(
                    op + "/architect",
                    partial(self.backend.architect, self._architect_context(incident),
                            self.workspace(parent)),
                )
                hidden_design_error = None
                try:
                    assert_no_hidden_fields(raw_design, where="Architect proposal")
                except AssertionError as exc:
                    hidden_design_error = str(exc)
                design = strip_hidden_fields(raw_design)
                report["design"] = design
                if isinstance(design, dict) and design.get("decision") == "NO_VIABLE_REPAIR":
                    write_json(attempt_dir / "design.json", design)
                    report["diagnosis"] = design.get("diagnosis")
                    incident = self._reject(
                        incident, report, attempt_idx, "architect_no_viable_repair",
                        str(design.get("diagnosis") or "Architect found no viable focused repair"))
                    self._block_stalled(
                        incident, "Architect reported no viable focused repair: " +
                        str(design.get("diagnosis") or "no diagnosis given"))
                report["diagnosis"] = design.get("diagnosis") if isinstance(design, dict) else None
                report["focused_goal"] = (
                    design.get("focused_goal") if isinstance(design, dict) else None)
                report["design_summary"] = (
                    design.get("work_order") if isinstance(design, dict) else None)
                try:
                    if hidden_design_error:
                        raise ValidationFailure(hidden_design_error, stage="diagnosis_review")
                    self._validate_focused_design(design)
                except ValidationFailure as exc:
                    review = {
                        "status": "rejected",
                        "checks": {"proposal_shape_is_focused": False},
                        "cited_trace_ids": [],
                        "cited_decision_rules": [],
                        "feedback": [str(exc)],
                        "duplicate_of_attempt": None,
                        "rejection_reason": "diagnosis_review_rejected",
                    }
                    report["diagnosis_review"] = review
                    report["gate_status"]["diagnosis_review"] = "rejected"
                    write_json(attempt_dir / "design.json", design)
                    write_json(attempt_dir / "diagnosis_review.json", review)
                    incident = self._transition(incident, "diagnosis_review")
                    incident = self._record_diagnosis_review(incident, review, attempt_idx)
                    self._checkpoint_attempt(index, attempt_idx, report)
                    incident = self._reject(
                        incident, report, attempt_idx, "diagnosis_review_rejected", str(exc))
                    continue
                report["diagnosis_signature"] = digest(self._normalize(design["diagnosis"]))
                report["design_signature"] = digest({
                    "focused_goal": self._normalize(design["focused_goal"]),
                    "work_order": [self._normalize(item) for item in design["work_order"]],
                })
                sql = str(design.get("migration_sql") or "")
                report["migration_signature"] = hashlib.sha256(
                    self._normalize(sql).encode()).hexdigest()
                write_json(attempt_dir / "design.json", design)
                previous = self._prior_reports(
                    incident, status={"rejected", "interrupted", "infrastructure_failed"})
                incident = self._transition(incident, "diagnosis_review")
                review = self._diagnosis_review(incident, design, report, previous)
                report["diagnosis_review"] = review
                report["gate_status"]["diagnosis_review"] = review["status"]
                write_json(attempt_dir / "diagnosis_review.json", review)
                incident = self._record_diagnosis_review(incident, review, attempt_idx)
                self._checkpoint_attempt(index, attempt_idx, report)
                if review["status"] != "accepted":
                    report.update({
                        "repetition_detected": bool(review.get("duplicate_of_attempt")),
                        "developer_skipped": True,
                        "duplicate_of_attempt": review.get("duplicate_of_attempt"),
                    })
                    incident = self._reject(
                        incident, report, attempt_idx, review["rejection_reason"],
                        "; ".join(review["feedback"]),
                    )
                    continue

                sequence = int(self.journal.get("version_sequence", 1)) + 1
                candidate = f"version_{sequence:04d}"
                report["candidate_system_version"] = candidate
                with self.journal.db:
                    self.journal.put("version_sequence", sequence)
                    self.journal.put("candidate_system_version", candidate)
                work = attempt_dir / "workspace"
                copy_workspace(self.workspace(parent), work)
                incident = self._transition(
                    incident, "developing", candidate_system_version=candidate)
                report["phase"] = "developing"
                self._checkpoint_attempt(index, attempt_idx, report)

                await self._develop_and_build(
                    incident, report, attempt_idx, op, work, design, attempt_dir)

                parent_manifest = code_manifest(self.workspace(parent))
                candidate_manifest = code_manifest(work)
                paths = sorted(set(parent_manifest) | set(candidate_manifest))
                report["files_changed"] = [path for path in paths
                                           if parent_manifest.get(path) != candidate_manifest.get(path)]
                report["source_diff_summary"] = [{
                    "path": path,
                    "before": parent_manifest.get(path),
                    "after": candidate_manifest.get(path),
                } for path in report["files_changed"]]
                report["source_diff_signature"] = digest(report["source_diff_summary"])
                duplicate_diff = next((item for item in reversed(previous)
                    if item.get("source_diff_signature") == report["source_diff_signature"]
                    and item.get("migration_signature") == report["migration_signature"]
                    and item.get("db_attempt_index") != attempt_idx), None)
                if duplicate_diff:
                    report["repetition_detected"] = True
                    report["duplicate_kind"] = "source_change"
                    report["duplicate_of_attempt"] = {
                        "boundary_index": duplicate_diff["boundary_index"],
                        "attempt_index": duplicate_diff["db_attempt_index"],
                    }
                    incident = self._reject(
                        incident, report, attempt_idx, "duplicate_source_change",
                        "candidate source diff and migration duplicate previously rejected work",
                    )
                    continue
                if candidate_manifest == parent_manifest and not sql.strip():
                    incident = self._reject(
                        incident, report, attempt_idx, "no_material_code_change",
                        "candidate made no material source or migration change",
                    )
                    continue

                try:
                    reject_checkpoint_specialization(
                        self.workspace(parent), work,
                        [visible_checkpoint(self.checkpoints[row["checkpoint_id"]])
                         for row in self.journal.official() if row.get("score")
                         and row["event_boundary_index"] <= index],
                        sql,
                    )
                    report["gate_status"]["no_checkpoint_specialization"] = "passed"
                except ValidationFailure as exc:
                    report["gate_status"]["no_checkpoint_specialization"] = "failed"
                    report["exact_errors"] = [str(exc)]
                    incident = self._reject(
                        incident, report, attempt_idx, "safety_regression", str(exc))
                    continue

                if incident["trigger_type"] == "scored_checkpoint_failure":
                    incident, promoted = await self._post_evaluation_candidate(
                        incident, report, attempt_idx, op, work, sql, attempt_dir)
                else:
                    incident, promoted = await self._pre_ingestion_candidate(
                        incident, report, attempt_idx, op, work, sql, attempt_dir, event, header)
                if promoted and incident.get("resolved"):
                    return True
                if promoted:
                    continue
            except InfrastructureFailure as exc:
                report["phase"] = "infrastructure_blocked"
                report["transport_failure"] = True
                report["infrastructure_error"] = str(exc)
                self._finish_attempt(index, attempt_idx, "infrastructure_failed", report)
                self._transition(incident, "infrastructure_blocked", infrastructure_error=str(exc))
                raise
            except DeveloperApproachUnworkable as exc:
                incident = self._reject(
                    incident, report, attempt_idx, "approach_unworkable", str(exc))
                continue
            except ValidationFailure as exc:
                report.setdefault("exact_errors", []).append(str(exc))
                report["validation_stage"] = exc.stage
                report["validation_origin"] = exc.origin
                report["protected_state_table"] = exc.table
                report["protected_state_difference"] = exc.row_difference
                reason = "safety_regression" if exc.stage in {
                    "data_preservation", "isolation", "authorization_evidence", "derived_evidence",
                } else ("no_material_code_change" if
                        "leaves implementation and database unchanged" in str(exc)
                        else "implementation_failure")
                incident = self._reject(incident, report, attempt_idx, reason, str(exc))
                continue
        return True

    async def _develop_and_build(
        self, incident, report, attempt_idx, op, work, design, attempt_dir,
    ):
        index = incident["boundary_index"]
        first = True
        rounds = []
        while True:
            report["phase"] = "developing"
            if design["decision"] == "DATA_MIGRATION":
                outcome = {"status": "not_needed_for_data_migration"}
            elif first:
                outcome = await self.operation(
                    op + "/developer/start", partial(self.backend.develop, work, design))
            else:
                feedback = rounds[-1]["feedback"]
                outcome = await self.operation(
                    op + f"/developer/correction_{len(rounds):02d}",
                    partial(self.backend.correct, work, design, feedback),
                )
            report["gate_status"]["developer"] = "passed"
            rounds.append({"kind": "developer_outcome", "result": outcome})
            write_json(attempt_dir / "developer_outcome.json", {"rounds": rounds})
            self._transition(
                incident, "build_validation",
                developer_correction_rounds=max(0, len(rounds) - 1),
            )
            report["phase"] = "build_validation"
            try:
                reports = await self.operation(
                    op + f"/build_validation_{len(rounds):02d}",
                    partial(
                        self._build_validation, work,
                        self.workspace("version_0001") / "tests",
                    ),
                )
            except ValidationFailure as exc:
                if "bubblewrap sandbox unavailable" in str(exc):
                    raise InfrastructureFailure(str(exc)) from exc
                gate_reports = exc.gate_reports or {}
                for name in ("compile", "invariants", "unit_tests"):
                    if name in gate_reports:
                        report["gate_status"][name] = (
                            "passed" if gate_reports[name].get("ok") else "failed")
                feedback = {
                    "stage": exc.origin if exc.origin in {
                        "compile", "invariants", "unit_tests"} else "build_validation",
                    "error": str(exc),
                    "gate_reports": gate_reports,
                }
                rounds[-1]["feedback"] = feedback
                report.setdefault("build_test_errors", []).append(feedback)
                if len(rounds) > self.options.max_correction_rounds:
                    report["build_validation"] = {
                        "status": "failed_after_correction_limit", "rounds": rounds,
                    }
                    self._checkpoint_attempt(index, attempt_idx, report)
                    write_json(attempt_dir / "build_validation.json", report["build_validation"])
                    raise ValidationFailure(
                        f"build still failing after {len(rounds) - 1} correction round(s): {exc}",
                        stage="build_validation", origin=feedback["stage"],
                        gate_reports=gate_reports) from exc
                report["build_validation"] = {
                    "status": "failed_and_returned_to_same_developer", "rounds": rounds,
                }
                self._checkpoint_attempt(index, attempt_idx, report)
                write_json(attempt_dir / "build_validation.json", report["build_validation"])
                self._transition(
                    incident, "developing", last_build_error=str(exc),
                    developer_correction_rounds=len(rounds),
                )
                first = False
                continue
            for name in ("compile", "invariants", "unit_tests"):
                report["gate_status"][name] = (
                    "passed" if name not in reports or reports[name].get("ok") else "failed")
            report["build_gates"] = reports
            report["build_validation"] = {
                "status": "passed", "rounds": rounds, "gate_reports": reports,
            }
            self._checkpoint_attempt(index, attempt_idx, report)
            write_json(attempt_dir / "build_validation.json", report["build_validation"])
            return

    async def _build_validation(self, workspace, trusted_tests):
        """Run sandboxed subprocesses on the event-loop thread.

        Bubblewrap may deadlock while forking from an executor worker. The
        gate process itself is bounded by its subprocess timeout, so running
        this small synchronous orchestration step here is deliberate.
        """
        return build_gates(workspace, trusted_tests)

    async def _post_evaluation_candidate(
        self, incident, report, attempt_idx, op, work, sql, attempt_dir,
    ):
        index = incident["boundary_index"]
        targets = [row for row in self.journal.official()
                   if row["checkpoint_id"] in set(
                       incident["unresolved_target_checkpoint_ids"])]
        incident = self._transition(incident, "targeted_replay")
        targeted = await self._paired_replay(
            index, attempt_idx, incident["safe_parent_version"], work, sql,
            targets, stage="targeted", op=op,
        )
        target_result = self._targeted_result(incident, targeted)
        report["targeted_replay"] = target_result
        report["gate_status"]["targeted_replay"] = (
            "passed" if target_result["status"] == "improved" else "failed")
        self._copy_replay_observability(report, target_result)
        write_json(attempt_dir / "targeted_replay.json", target_result)
        self._checkpoint_attempt(index, attempt_idx, report)
        if target_result["status"] != "improved":
            report["broad_validation"] = {
                "status": "skipped", "reason": "no_measurable_improvement",
            }
            write_json(attempt_dir / "broad_validation.json", report["broad_validation"])
            if target_result.get("behavioral_duplicate_of"):
                report["duplicate_kind"] = "replay_behavior"
            reason = ("duplicate_replay_behavior" if target_result.get("behavioral_duplicate_of")
                      else "no_measurable_improvement")
            return self._reject(
                incident, report, attempt_idx, reason,
                "targeted replay produced no failing-to-passing transition",
            ), False

        incident = self._transition(incident, "broad_validation")
        try:
            migrated = await self._prepare_migrated_images(
                index, incident["safe_parent_version"], work, sql, op, report)
        except ValidationFailure as exc:
            self._mark_validation_failure(report, exc)
            rejection = ("no_material_code_change" if
                         "leaves implementation and database unchanged" in str(exc)
                         else "safety_regression")
            report["broad_validation"] = {
                "status": "failed", "reason": rejection, "error": str(exc),
            }
            return self._reject(
                incident, report, attempt_idx, rejection, str(exc)), False

        target_ids = set(incident["unresolved_target_checkpoint_ids"])
        broad_rows = [row for row in self.journal.official()
                      if row.get("score") and row["event_boundary_index"] <= index
                      and row["checkpoint_id"] not in target_ids]
        broad_pairs = await self._paired_replay(
            index, attempt_idx, incident["safe_parent_version"], work, sql,
            broad_rows, stage="broad", op=op,
        )
        broad = self._broad_result(self._combine_replays(targeted, broad_pairs))
        report["broad_validation"] = broad
        report["gate_status"]["broad_validation"] = broad["status"]
        for gate, status in broad["gates"].items():
            report["gate_status"][gate] = "passed" if status else "failed"
        write_json(attempt_dir / "broad_validation.json", broad)
        self._checkpoint_attempt(index, attempt_idx, report)
        if broad["status"] != "passed":
            return self._reject(
                incident, report, attempt_idx, "safety_regression",
                "; ".join(broad["regressions"]) or "broad mandatory gate failed",
            ), False
        return await self._promote(
            incident, report, attempt_idx, work, sql, migrated,
            staged_image=None, event=None, target_gains=target_result["failing_to_passing"],
        ), True

    async def _pre_ingestion_candidate(
        self, incident, report, attempt_idx, op, work, sql, attempt_dir, event, header,
    ):
        index = incident["boundary_index"]
        staged = incident["staged_input"]
        if digest(staged) != incident["staged_input_digest"]:
            raise ValidationFailure("exact staged input digest changed")
        incident = self._transition(incident, "targeted_replay")
        row = self.journal.db.execute(
            "SELECT * FROM episodes WHERE id=?", (staged["episode_id"],)).fetchone()
        if row:
            incoming = migrate_blob(row["memory"], sql)
        else:
            empty, _ = await self.operation(
                op + "/targeted/empty_parent",
                partial(self.backend.memory, self.workspace(incident["safe_parent_version"]),
                        None, header, delta=[]),
            )
            empty = self._migrate_to(empty, "version_0001", incident["safe_parent_version"])
            incoming = migrate_blob(empty, sql)
        try:
            staged_image, _ = await self.operation(
                op + "/targeted/exact_staged_input",
                partial(self.backend.memory, work, incoming, header, delta=staged["turns"]),
            )
            self._isolation(staged_image, staged["episode_id"], header)
            self._validate_delta_ingested(staged_image, staged["episode_id"], staged["turns"])
        except (ValidationFailure, sqlite3.Error) as exc:
            report["gate_status"]["exact_staged_input"] = "failed"
            report["gate_status"]["targeted_replay"] = "failed"
            report["targeted_replay"] = {
                "status": "no_improvement",
                "exact_staged_input_digest": incident["staged_input_digest"],
                "error": str(exc),
            }
            report["broad_validation"] = {
                "status": "skipped", "reason": "no_measurable_improvement",
            }
            write_json(attempt_dir / "targeted_replay.json", report["targeted_replay"])
            return self._reject(
                incident, report, attempt_idx, "no_measurable_improvement", str(exc)), False
        report["gate_status"]["exact_staged_input"] = "passed"
        report["gate_status"]["targeted_replay"] = "passed"
        report["targeted_replay"] = {
            "status": "improved",
            "exact_staged_input_digest": incident["staged_input_digest"],
            "represented_turn_ids": [turn["turn_id"] for turn in staged["turns"]],
        }
        write_json(attempt_dir / "targeted_replay.json", report["targeted_replay"])
        self._checkpoint_attempt(index, attempt_idx, report)

        incident = self._transition(incident, "broad_validation")
        try:
            migrated = await self._prepare_migrated_images(
                index, incident["safe_parent_version"], work, sql, op, report)
        except ValidationFailure as exc:
            self._mark_validation_failure(report, exc)
            rejection = ("no_material_code_change" if
                         "leaves implementation and database unchanged" in str(exc)
                         else "safety_regression")
            report["gate_status"]["broad_validation"] = "failed"
            report["broad_validation"] = {
                "status": "failed", "reason": rejection, "error": str(exc),
            }
            write_json(attempt_dir / "broad_validation.json", report["broad_validation"])
            return self._reject(
                incident, report, attempt_idx, rejection, str(exc)), False
        rows = [row for row in self.journal.official()
                if row.get("score") and row["event_boundary_index"] <= index]
        broad = self._broad_result(await self._paired_replay(
            index, attempt_idx, incident["safe_parent_version"], work, sql,
            rows, stage="broad", op=op,
        ))
        report["broad_validation"] = broad
        report["gate_status"]["broad_validation"] = broad["status"]
        for gate, status in broad["gates"].items():
            report["gate_status"][gate] = "passed" if status else "failed"
        write_json(attempt_dir / "broad_validation.json", broad)
        self._checkpoint_attempt(index, attempt_idx, report)
        if broad["status"] != "passed":
            return self._reject(
                incident, report, attempt_idx, "safety_regression",
                "; ".join(broad["regressions"]) or "broad mandatory gate failed",
            ), False
        migrated[staged["episode_id"]] = staged_image
        return await self._promote(
            incident, report, attempt_idx, work, sql, migrated,
            staged_image=staged_image, event=event, target_gains=[],
        ), True

    # Replay and broad validation.
    @staticmethod
    def _score_view(score):
        return {key: score.get(key) for key in (
            "category", "passed", "utility_correct", "privacy_leak", "deletion_leak")}

    @staticmethod
    def _behavior(prediction, score):
        output = prediction.get("output", {})
        safe = replay_behavior(output)
        answer = safe["rendered_answer"]
        return {**safe,
                "rendered_answer_hash": hashlib.sha256(
                    " ".join(answer.split()).encode()).hexdigest(),
                "passed": bool(score.get("passed"))}

    async def _paired_replay(self, index, attempt, parent, work, sql, rows, *, stage, op):
        pairs = {}
        repairs = []
        for row in rows:
            checkpoint_id = row["checkpoint_id"]
            boundary = row["event_boundary_index"]
            checkpoint = self.checkpoints[checkpoint_id]
            header = episode_header(self.dataset.episodes_by_id[checkpoint["episode_id"]])
            if self.options.replay_reingest:
                position = self.schedule[boundary - 1]["turn_position"]
                key = f"{op}/{stage}_replay/{checkpoint_id}"
                image = await self._rebuilt_image(
                    self.workspace(parent), parent, "", checkpoint["episode_id"], position,
                    key + "/parent/rebuild")
                candidate_image = await self._rebuilt_image(
                    work, parent, sql, checkpoint["episode_id"], position,
                    key + "/candidate/rebuild")
            else:
                _, event_state, image = self.journal.event(boundary)
                image = self._migrate_to(image, event_state["frozen_version"], parent)
                candidate_image = migrate_blob(image, sql)
            results = {}
            for label, workspace, blob in (
                ("parent", self.workspace(parent), image),
                ("candidate", work, candidate_image),
            ):
                votes = []
                for vote in range(self.options.replay_votes):
                    suffix = f"/vote_{vote + 1:02d}" if self.options.replay_votes > 1 else ""
                    prediction = await self.operation(
                        f"{op}/{stage}_replay/{checkpoint_id}/{label}/predict{suffix}",
                        partial(self.backend.predict, workspace, blob, header,
                                visible_checkpoint(checkpoint)),
                    )
                    assert_no_hidden_fields(prediction, where=f"{stage} evaluator prediction")
                    score = await self.operation(
                        f"{op}/{stage}_replay/{checkpoint_id}/{label}/judge{suffix}",
                        partial(self.backend.judge, prediction, checkpoint),
                    )
                    votes.append((prediction, score))
                passes = sum(bool(score.get("passed")) for _, score in votes)
                majority = passes * 2 > len(votes)
                # Report the first prediction that agrees with the majority.
                prediction, score = next(item for item in votes
                                         if bool(item[1].get("passed")) == majority)
                results[label] = {
                    "prediction": prediction,
                    "score": {**self._score_view(score), "votes_passed": passes,
                              "votes": len(votes)},
                    "behavior": self._behavior(prediction, score),
                }
            parent_behavior = results["parent"]["behavior"]
            candidate_behavior = results["candidate"]["behavior"]
            pair = {
                "checkpoint_id": checkpoint_id,
                "official_failed": not bool(row["score"]["passed"]),
                "parent": parent_behavior,
                "candidate": candidate_behavior,
                "evidence_changed": (parent_behavior["authorized_evidence"] !=
                                     candidate_behavior["authorized_evidence"]),
                "decision_trace_changed": (parent_behavior["decision_trace"] !=
                                           candidate_behavior["decision_trace"]),
                "answer_changed": (
                    parent_behavior["action"], parent_behavior["rendered_answer_hash"]
                ) != (
                    candidate_behavior["action"], candidate_behavior["rendered_answer_hash"]
                ),
                "score_changed": parent_behavior["passed"] != candidate_behavior["passed"],
                "transition": (
                    "failing_to_passing" if not parent_behavior["passed"] and
                    candidate_behavior["passed"] else
                    "passing_to_failing" if parent_behavior["passed"] and
                    not candidate_behavior["passed"] else "unchanged_pass" if
                    parent_behavior["passed"] else "unchanged_fail"
                ),
            }
            pair["differential"] = {
                "action": {"before": parent_behavior["action"],
                           "after": candidate_behavior["action"]},
                "rendered_answer": {"before": parent_behavior["rendered_answer"],
                                    "after": candidate_behavior["rendered_answer"]},
                "authorized_evidence": {
                    "before": parent_behavior["authorized_evidence"],
                    "after": candidate_behavior["authorized_evidence"],
                },
                "decision_trace": {"before": parent_behavior["decision_trace"],
                                   "after": candidate_behavior["decision_trace"]},
                "pass_fail": {"before": parent_behavior["passed"],
                              "after": candidate_behavior["passed"],
                              "transition": pair["transition"]},
            }
            pairs[checkpoint_id] = pair
            repairs.append({
                "attempt_kind": "repair_attempt",
                "replay_stage": stage,
                "checkpoint_id": checkpoint_id,
                "event_boundary_index": boundary,
                "repair_boundary_index": index,
                "repair_attempt_index": attempt,
                "candidate_system_version": self.journal.get("candidate_system_version"),
                "prediction": results["candidate"]["prediction"],
                "score": results["candidate"]["score"],
            })
        with self.journal.db:
            for repair in repairs:
                self.journal.db.execute(
                    "INSERT OR REPLACE INTO repairs VALUES (?, ?, ?, ?)",
                    (index, attempt, repair["checkpoint_id"], encode(repair)),
                )
        signature = digest({
            "pairs": {checkpoint_id: pair["candidate"] for checkpoint_id, pair in pairs.items()},
            "failed_checkpoint_ids": sorted(
                checkpoint_id for checkpoint_id, pair in pairs.items()
                if not pair["candidate"]["passed"]),
        })
        return {"stage": stage, "pairs": pairs, "behavioral_signature": signature}

    def _targeted_result(self, incident, replay):
        pairs = replay["pairs"]
        gains = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                       if pair["transition"] == "failing_to_passing")
        losses = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                        if pair["transition"] == "passing_to_failing")
        failed = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                        if not pair["candidate"]["passed"])
        evidence_changed = any(pair["evidence_changed"] for pair in pairs.values())
        trace_changed = any(pair["decision_trace_changed"] for pair in pairs.values())
        answer_changed = any(pair["answer_changed"] for pair in pairs.values())
        score_changed = any(pair["score_changed"] for pair in pairs.values())
        unchanged = not evidence_changed and not trace_changed and not answer_changed and not score_changed
        previous = None
        for item in reversed(self._prior_reports(incident, status={"rejected"})):
            if item.get("behavioral_signature") == replay["behavioral_signature"]:
                previous = {"boundary_index": item["boundary_index"],
                            "attempt_index": item["db_attempt_index"]}
                break
        return {
            "status": "improved" if gains and not losses else "no_improvement",
            "pairs": pairs,
            "failing_to_passing": gains,
            "passing_to_failing": losses,
            "failed_checkpoint_ids": failed,
            "evidence_changed": evidence_changed,
            "decision_trace_changed": trace_changed,
            "answer_changed": answer_changed,
            "score_changed": score_changed,
            "replay_unchanged": unchanged,
            "behavioral_signature": replay["behavioral_signature"],
            "behavioral_duplicate_of": previous,
        }

    @staticmethod
    def _combine_replays(*replays):
        pairs = {}
        for replay in replays:
            pairs.update(replay["pairs"])
        return {"stage": "combined", "pairs": pairs,
                "behavioral_signature": digest({
                    "pairs": {key: value["candidate"] for key, value in pairs.items()}})}

    def _broad_result(self, replay):
        pairs = replay["pairs"]
        def gate_score(checkpoint_id, passed):
            category = self.checkpoints[checkpoint_id]["query_type"]
            return {
                "category": category,
                "passed": passed,
                "utility_correct": passed if category == "utility" else None,
                "privacy_leak": not passed if category == "privacy" else None,
                "deletion_leak": not passed if category == "safety" else None,
            }
        baseline = {checkpoint_id: gate_score(checkpoint_id, pair["parent"]["passed"])
                    for checkpoint_id, pair in pairs.items()}
        candidate = {checkpoint_id: gate_score(checkpoint_id, pair["candidate"]["passed"])
                     for checkpoint_id, pair in pairs.items()}
        regressions = promotion_regressions(
            baseline, candidate, self.options.utility_tolerance) if pairs else []
        privacy = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                         if baseline[checkpoint_id].get("category") == "privacy"
                         and pair["transition"] == "passing_to_failing")
        deletion = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                          if baseline[checkpoint_id].get("category") == "safety"
                          and pair["transition"] == "passing_to_failing")
        utility = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                         if baseline[checkpoint_id].get("category") == "utility"
                         and pair["transition"] == "passing_to_failing")
        metrics = candidate_metrics(candidate, minimum_coverage=1) if candidate else {
            "counts": {key: {"n": 0, "pass": 0}
                       for key in ("utility", "privacy", "safety")},
            "U": None, "A": None, "F": None, "MGS": None,
            "metric_coverage_sufficient": False,
        }
        return {
            "status": "passed" if not regressions else "failed",
            "pairs": pairs,
            "regressions": regressions,
            "privacy_regressions": privacy,
            "deletion_regressions": deletion,
            "utility_regressions": utility,
            "failing_to_passing": sorted(
                checkpoint_id for checkpoint_id, pair in pairs.items()
                if pair["transition"] == "failing_to_passing"),
            "passing_to_failing": sorted(
                checkpoint_id for checkpoint_id, pair in pairs.items()
                if pair["transition"] == "passing_to_failing"),
            "failed_checkpoint_ids": sorted(
                checkpoint_id for checkpoint_id, pair in pairs.items()
                if not pair["candidate"]["passed"]),
            "behavioral_signature": replay["behavioral_signature"],
            "metrics": metrics,
            "gates": {
                "privacy": not privacy,
                "deletion": not deletion,
                "utility": not any("utility regression" in reason for reason in regressions),
            },
        }

    @staticmethod
    def _copy_replay_observability(report, result):
        report.update({
            "failed_checkpoint_ids": result.get("failed_checkpoint_ids"),
            "paired_gains": result.get("failing_to_passing", []),
            "passing_to_failing": result.get("passing_to_failing", []),
            "behavioral_signature": result.get("behavioral_signature"),
            "replay_unchanged": result.get("replay_unchanged"),
            "evidence_changed": result.get("evidence_changed"),
            "decision_trace_changed": result.get("decision_trace_changed"),
            "answer_changed": result.get("answer_changed"),
            "score_changed": result.get("score_changed"),
        })

    async def _prepare_migrated_images(self, index, parent, work, sql, op, report):
        migrated = {}
        for row in self.journal.db.execute("SELECT * FROM episodes"):
            image = migrate_blob(row["memory"], sql)
            report["gate_status"]["migration"] = "passed"
            episode_info = episode_header(self.dataset.episodes_by_id[row["id"]])
            if self.options.replay_reingest:
                # The committed image is re-derived from the same turns; raw
                # observations must match and no tombstone may be lost.
                reopened = await self._rebuilt_image(
                    work, parent, sql, row["id"], row["cursor"], op + "/broad/rebuild/" + row["id"])
                self._preserve_rebuilt(row["memory"], reopened)
            else:
                reopened, _ = await self.operation(
                    op + "/broad/open/" + row["id"],
                    partial(self.backend.memory, work, image, episode_info, delta=[]),
                )
                self._preserve_images(image, reopened)
            report["gate_status"]["data_preservation"] = "passed"
            self._isolation(reopened, row["id"], episode_info)
            report["gate_status"]["database_integrity"] = "passed"
            report["gate_status"]["episode_isolation"] = "passed"
            report["gate_status"]["patient_isolation"] = "passed"
            migrated[row["id"]] = reopened
        current = self.schedule[index - 1]
        info = episode_header(self.dataset.episodes_by_id[current["episode_id"]])
        empty, _ = await self.operation(
            op + "/broad/empty_parent",
            partial(self.backend.memory, self.workspace(parent), None, info, delta=[]),
        )
        empty = self._migrate_to(empty, "version_0001", parent)
        empty_migrated = migrate_blob(empty, sql)
        report["gate_status"]["migration"] = "passed"
        if (code_manifest(work) == code_manifest(self.workspace(parent)) and
                image_signature(empty) == image_signature(empty_migrated) and
                all(image_signature(row["memory"]) == image_signature(migrated[row["id"]])
                    for row in self.journal.db.execute("SELECT * FROM episodes"))):
            raise ValidationFailure("candidate leaves implementation and database unchanged")
        return migrated

    @staticmethod
    def _mark_validation_failure(report, exc):
        report["validation_stage"] = exc.stage
        report["validation_origin"] = exc.origin
        report["protected_state_table"] = exc.table
        report["protected_state_difference"] = exc.row_difference
        report.setdefault("exact_errors", []).append(str(exc))
        mapping = {
            "data_preservation": "data_preservation",
            "isolation": "episode_isolation",
            "authorization_evidence": "data_preservation",
            "derived_evidence": "data_preservation",
        }
        if exc.stage in mapping:
            report["gate_status"][mapping[exc.stage]] = "failed"

    # Promotion, rejection, and structured feedback.
    async def _promote(
        self, incident, report, attempt_idx, work, sql, migrated,
        *, staged_image, event, target_gains,
    ):
        index = incident["boundary_index"]
        incident = self._transition(incident, "promotion")
        report["phase"] = "promotion"
        report["promotion_eligible"] = True
        report["usage"] = self._attempt_usage(index, attempt_idx)
        report["elapsed_s"] = time.time() - report["started_at"]
        candidate = report["candidate_system_version"]
        parent = incident["safe_parent_version"]
        destination = self.workspace(candidate)
        copy_workspace(work, destination)
        staged_turns = (incident.get("staged_input") or {}).get("turns") or []
        manifest = {
            **self.version_manifest(parent),
            "parent": parent,
            "schema_version": self.version_manifest(parent)["schema_version"] + int(bool(sql.strip())),
            "migration_sql": sql,
            "code": code_manifest(destination),
            "status": "committed",
            "promoted_after_boundary": index if incident["trigger_type"] ==
            "scored_checkpoint_failure" else index - 1,
            "supported_turn_fields": sorted(
                set(self.version_manifest(parent)["supported_turn_fields"]) |
                (set().union(*(set(turn) for turn in staged_turns)) if staged_turns else set())
            ),
        }
        write_json(destination.parent / "manifest.json", manifest)
        write_json(destination.parent / "parent.json", {"parent": parent})
        write_json(destination.parent / "promotion_report.json", report)
        self._verify_version(parent)

        remaining_ids = set(incident.get("unresolved_target_checkpoint_ids") or [])
        remaining_ids -= set(target_gains)
        attempt_ref = self._attempt_ref(report, "promoted")
        promotions = list(incident.get("promotion_history") or []) + [attempt_ref]
        with self.journal.db:
            for episode_id, blob in migrated.items():
                if event is not None and episode_id == event["episode_id"]:
                    continue
                self.journal.db.execute(
                    "UPDATE episodes SET memory=? WHERE id=?", (blob, episode_id))
            if event is not None:
                self.journal.db.execute(
                    "INSERT OR REPLACE INTO episodes VALUES (?, ?, ?)",
                    (event["episode_id"], event["turn_position"], staged_image),
                )
                staged_state = self.journal.event(index)[1]
                self.journal.save_event(
                    index, "ingested", {**staged_state, "frozen_version": candidate}, staged_image)
            versions = self.journal.get("versions")
            versions[candidate] = manifest
            self.journal.put("versions", versions)
            self.journal.put("committed_system_version", candidate)
            self.journal.put("candidate_system_version", None)
            self.journal.db.execute(
                "UPDATE attempts SET status='promoted',value=? WHERE boundary=? AND idx=?",
                (encode(report), index, attempt_idx),
            )
            if target_gains:
                self._resolve_pending_gains(target_gains, index)
        write_json(
            self.run_dir / "events" / f"boundary_{index:04d}" / "attempts" /
            f"attempt_{attempt_idx:02d}" / "validation.json",
            {**report, "status": "promoted"},
        )
        self._log_attempt_result(report, "promoted")

        if incident["trigger_type"] == "pre_ingestion" or not remaining_ids:
            incident = {
                **incident,
                "safe_parent_version": candidate,
                "unresolved_target_checkpoint_ids": [],
                "candidate_attempt_history": list(incident.get("candidate_attempt_history") or []) +
                [attempt_ref],
                "promotion_history": promotions,
                "phase": "promotion",
                "resolved": True,
                "resolution": "promoted",
                "updated_at": time.time(),
            }
            self._save_incident(incident)
            return incident

        incident = {
            **incident,
            "safe_parent_version": candidate,
            "unresolved_target_checkpoint_ids": sorted(remaining_ids),
            "candidate_attempt_history": list(incident.get("candidate_attempt_history") or []) +
            [attempt_ref],
            "promotion_history": promotions,
            "structured_feedback_history": [],
            "candidate_system_version": None,
            "stalled_rejections": 0,
            "duplicate_rejections": 0,
        }
        return self._transition(
            incident, "architect", partial_progress=True,
            last_paired_gains=sorted(target_gains),
        )

    def _reject(self, incident, report, attempt_idx, reason, detail):
        report["phase"] = "rejected_with_feedback"
        report["promotion_eligible"] = False
        report["rejection_reason"] = reason
        report["failed_gate"] = reason
        report["reasons"] = list(dict.fromkeys((report.get("reasons") or []) + [detail]))
        report["usage"] = self._attempt_usage(incident["boundary_index"], attempt_idx)
        report["elapsed_s"] = time.time() - report["started_at"]
        feedback = self._structured_feedback(incident, report)
        report["structured_feedback"] = feedback
        attempt_dir = (self.run_dir / "events" / f"boundary_{incident['boundary_index']:04d}" /
                       "attempts" / f"attempt_{attempt_idx:02d}")
        write_json(attempt_dir / "structured_feedback.json", feedback)
        self._finish_attempt(incident["boundary_index"], attempt_idx, "rejected", report)
        history = list(incident.get("candidate_attempt_history") or []) + [
            self._attempt_ref(report, "rejected")]
        feedback_history = list(incident.get("structured_feedback_history") or []) + [feedback]
        incident = {
            **incident,
            "candidate_attempt_history": history,
            "structured_feedback_history": feedback_history,
            "candidate_system_version": None,
            "last_rejection_reason": reason,
            "stalled_rejections": int(incident.get("stalled_rejections") or 0) + 1,
            "duplicate_rejections": (int(incident.get("duplicate_rejections") or 0) +
                                     int(reason in _DUPLICATE_REASONS)),
        }
        incident = self._transition(incident, "rejected_with_feedback")
        return self._transition(incident, "architect")

    def _raise_if_stalled(self, incident):
        stalled = int(incident.get("stalled_rejections") or 0)
        duplicates = int(incident.get("duplicate_rejections") or 0)
        if stalled >= self.options.max_stalled_attempts:
            self._block_stalled(
                incident, f"{stalled} consecutive rejected candidates without a promotion")
        if duplicates >= self.options.max_duplicate_proposals:
            self._block_stalled(
                incident, f"{duplicates} proposals duplicated earlier rejected work")

    def _block_stalled(self, incident, why):
        reason = (f"repair incident {incident['incident_id']} stalled at boundary "
                  f"{incident['boundary_index']}: {why.rstrip('.')}. Resume with --retry-blocked to try "
                  "again, or --abandon-blocked to record the targets as unrepaired and advance.")
        self._transition(incident, "stalled", stall_reason=why)
        with self.journal.db:
            self.journal.put("status", "repair_blocked")
            self.journal.put("halt_reason", reason)
        log.warning(reason)
        raise RepairBlocked(reason)

    def _abandon_incident(self, incident):
        """Record a stalled scored incident's targets as unrepaired and release the stream."""
        if incident["trigger_type"] != "scored_checkpoint_failure":
            raise RepairBlocked(
                "only a scored incident can be abandoned; a staged delta that cannot be "
                "ingested must be repaired or the run restarted")
        targets = set(incident.get("unresolved_target_checkpoint_ids") or [])
        pending = [{**item, "abandoned": True,
                    "abandoned_at_boundary": incident["boundary_index"]}
                   if item["checkpoint_id"] in targets else item
                   for item in self.journal.get("pending_repairs", [])]
        with self.journal.db:
            self.journal.put("pending_repairs", pending)
        self._save_incident({
            **incident, "phase": "abandoned", "resolved": True,
            "resolution": "abandoned_after_stall", "updated_at": time.time(),
        })
        log.warning("repair incident %s abandoned; %d target(s) recorded as unrepaired",
                    incident["incident_id"], len(targets))

    @staticmethod
    def _attempt_ref(report, status):
        return {
            "boundary_index": report.get("boundary_index"),
            "attempt_index": report.get("attempt_index"),
            "candidate_number": report.get("candidate_number"),
            "safe_parent_version": report.get("safe_parent_version"),
            "candidate_system_version": report.get("candidate_system_version"),
            "status": status,
            "diagnosis_signature": report.get("diagnosis_signature"),
            "design_signature": report.get("design_signature"),
            "migration_signature": report.get("migration_signature"),
            "source_diff_signature": report.get("source_diff_signature"),
            "behavioral_signature": report.get("behavioral_signature"),
            "rejection_reason": report.get("rejection_reason"),
        }

    def _structured_feedback(self, incident, report):
        targeted = report.get("targeted_replay") or {"status": "not_run"}
        broad = report.get("broad_validation") or {"status": "not_run"}
        broad_for_architect = {key: broad.get(key) for key in (
            "status", "pairs", "failing_to_passing", "passing_to_failing",
            "failed_checkpoint_ids", "behavioral_signature",
        ) if key in broad}
        broad_for_architect["regression_count"] = len(broad.get("regressions") or [])
        previous = [item for item in incident.get("candidate_attempt_history", [])
                    if item.get("status") in {"rejected", "interrupted"}]
        feedback = {
            "format_version": 1,
            "incident_id": incident["incident_id"],
            "repair_objective": incident["repair_objective"],
            "triggering_boundary": incident["boundary_index"],
            "failure_type": incident["trigger_type"],
            "safe_parent_version": report.get("safe_parent_version"),
            "diagnosis_attempted": report.get("diagnosis"),
            "focused_goal_attempted": report.get("focused_goal"),
            "work_order_attempted": report.get("design_summary"),
            "files_changed": report.get("files_changed", []),
            "build_and_test_results": report.get("build_validation", {"status": "not_run"}),
            "exact_build_test_errors": report.get("build_test_errors", []),
            "gate_status": report.get("gate_status", {}),
            "diagnosis_review": report.get("diagnosis_review", {"status": "not_run"}),
            "failure_classification": {
                "transport_failure": bool(report.get("transport_failure")),
                "infrastructure_failure": bool(report.get("transport_failure")),
                "no_material_code_change": report.get("rejection_reason") == "no_material_code_change",
                "duplicate_proposal": (
                    report.get("rejection_reason") == "equivalent_previous_attempt"),
                "duplicate_source_change": (
                    report.get("rejection_reason") == "duplicate_source_change"),
                "duplicate_replay_behavior": (
                    report.get("rejection_reason") == "duplicate_replay_behavior"),
                "diagnosis_review_rejection": (
                    report.get("rejection_reason") == "diagnosis_review_rejected"),
                "build_or_test_failure": any(
                    report.get("gate_status", {}).get(key) == "failed"
                    for key in ("compile", "invariants", "unit_tests")),
                "implementation_failure": report.get("rejection_reason") == "implementation_failure",
                "ineffective_design": report.get("rejection_reason") == "no_measurable_improvement",
                "safety_regression": report.get("rejection_reason") == "safety_regression",
                "tests_ran_and_failed": sorted(
                    key for key in ("compile", "invariants", "unit_tests")
                    if report.get("gate_status", {}).get(key) == "failed"),
                "tests_never_ran": sorted(
                    key for key in ("compile", "invariants", "unit_tests")
                    if report.get("gate_status", {}).get(key) == "not_run"),
                "gates_never_ran": sorted(
                    key for key, value in report.get("gate_status", {}).items()
                    if value == "not_run"),
            },
            "targeted_replay": targeted,
            "broad_validation": broad_for_architect,
            "evidence_changed": targeted.get("evidence_changed"),
            "decision_trace_changed": targeted.get("decision_trace_changed"),
            "rendered_answer_changed": targeted.get("answer_changed"),
            "score_changed": targeted.get("score_changed"),
            "failing_to_passing": targeted.get("failing_to_passing", []),
            "passing_to_failing": (
                targeted.get("passing_to_failing", []) + broad.get("passing_to_failing", [])),
            "remaining_failed_checkpoints": targeted.get("failed_checkpoint_ids"),
            "regressions": {
                "checkpoint_transitions": broad.get("passing_to_failing", []),
                "preservation": report.get("protected_state_difference"),
                "isolation": report.get("gate_status", {}).get("episode_isolation") == "failed",
            },
            "behavioral_signature": report.get("behavioral_signature"),
            "source_diff_signature": report.get("source_diff_signature"),
            "design_signature": report.get("design_signature"),
            "diagnosis_signature": report.get("diagnosis_signature"),
            "migration_signature": report.get("migration_signature"),
            "token_usage": report.get("usage", {}).get("tokens", 0),
            "time_usage_s": report.get("usage", {}).get("seconds", 0.0),
            "previous_approaches_already_attempted": previous,
            "rejection_reason": report.get("rejection_reason"),
            "rejection_detail": (report.get("reasons") or [None])[-1],
        }
        assert_no_hidden_fields(feedback, where="Architect rejection feedback")
        return feedback

    @staticmethod
    def _feedback(report):
        return report.get("structured_feedback") or {
            "rejection_reason": report.get("rejection_reason") or report.get("failed_gate"),
            "gate_status": report.get("gate_status", {}),
            "exact_build_test_errors": report.get("build_test_errors", []),
            "safe_parent_version": report.get("safe_parent_version"),
            "diagnosis_attempted": report.get("diagnosis"),
            "work_order_attempted": report.get("design_summary"),
        }

    def _finish_attempt(self, index, count, status, report):
        report["usage"] = self._attempt_usage(index, count)
        with self.journal.db:
            self.journal.db.execute(
                "UPDATE attempts SET status=?,value=? WHERE boundary=? AND idx=?",
                (status, encode(report), index, count),
            )
            self.journal.put("candidate_system_version", None)
        path = (self.run_dir / "events" / f"boundary_{index:04d}" /
                "attempts" / f"attempt_{count:02d}")
        write_json(path / "validation.json", {**report, "status": status})
        self._log_attempt_result(report, status)

    @staticmethod
    def _log_attempt_result(report, status):
        targeted = report.get("targeted_replay") or {}
        broad = report.get("broad_validation") or {}
        remaining_value = targeted.get("failed_checkpoint_ids")
        remaining = "unknown" if remaining_value is None else str(len(remaining_value))
        gates = report.get("gate_status", {})
        build_values = [gates.get(name, "not_run")
                        for name in ("compile", "invariants", "unit_tests")]
        build = "failed" if "failed" in build_values else (
            "passed" if all(value == "passed" for value in build_values) else "not_run")
        log.info(
            "repair incident %s candidate %s: focused_goal=%s build=%s "
            "targeted_replay=%s broad_validation=%s evidence_changed=%s "
            "answer_changed=%s score_changed=%s failed_remaining=%s reason=%s action=%s",
            report.get("incident_id"), report.get("candidate_number"),
            report.get("focused_goal"), build, targeted.get("status", "not_run"),
            broad.get("status", "not_run"), targeted.get("evidence_changed"),
            targeted.get("answer_changed"), targeted.get("score_changed"), remaining,
            report.get("rejection_reason") or ("promoted" if status == "promoted" else "unknown"),
            "promoted" if status == "promoted" else "return_to_architect",
        )

    def _checkpoint_attempt(self, index, count, report):
        with self.journal.db:
            self.journal.db.execute(
                "UPDATE attempts SET value=? WHERE boundary=? AND idx=?",
                (encode(report), index, count),
            )
        path = (self.run_dir / "events" / f"boundary_{index:04d}" /
                "attempts" / f"attempt_{count:02d}")
        write_json(path / "validation.json", {**report, "status": "building"})

    def _resolve_pending_gains(self, checkpoint_ids, index):
        gained = set(checkpoint_ids)
        pending = [{**item, "resolved": True, "resolved_at_boundary": index}
                   if item["checkpoint_id"] in gained else item
                   for item in self.journal.get("pending_repairs", [])]
        self.journal.put("pending_repairs", pending)

    # Immutable version/image helpers.
    def _verify_version(self, version):
        if code_manifest(self.workspace(version)) != self.version_manifest(version)["code"]:
            raise ValidationFailure("committed workspace changed outside promotion")

    @staticmethod
    def _isolation(blob, episode_id, header):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            path.write_bytes(blob)
            with sqlite3.connect(path) as connection:
                if (connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or
                        list(connection.execute("PRAGMA foreign_key_check"))):
                    raise ValidationFailure(
                        "database integrity failed", stage="isolation", origin="runtime_derivation")
                ids = {row[0] for table in (
                    "observations", "memories", "relationships", "permissions", "semantic_links")
                    for row in connection.execute(
                        f"SELECT DISTINCT episode_id FROM {table}"
                    )}
                observation_ids = {row[0] for row in connection.execute(
                    "SELECT observation_id FROM observations")}
                memory_ids = {row[0] for row in connection.execute(
                    "SELECT memory_id FROM memories")}
                memory_sources = {row[0] for row in connection.execute(
                    "SELECT source_observation_id FROM memories")}
                permission_sources = {row[0] for row in connection.execute(
                    "SELECT source_observation_id FROM permissions")}
                semantic_sources = {row[0] for row in connection.execute(
                    "SELECT source_observation_id FROM semantic_links")}
                indexed_ids = {row[0] for row in connection.execute(
                    "SELECT DISTINCT memory_id FROM memory_terms")}
                known = {principal["principal_id"]
                         for principal in header.get("entities", {}).get("principals", [])}
                endpoints = {value for row in connection.execute(
                    "SELECT subject_id,object_id FROM relationships") for value in row if value}
            if (ids - {episode_id} or
                    (memory_sources | permission_sources | semantic_sources) - observation_ids or
                    indexed_ids - memory_ids or endpoints - known):
                raise ValidationFailure(
                    "episode/entity isolation failed", stage="isolation",
                    origin="runtime_derivation")

    @staticmethod
    def _validate_delta_ingested(blob, episode_id, delta):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            path.write_bytes(blob)
            with sqlite3.connect(path) as connection:
                found = {row[1]: row for row in connection.execute(
                    "SELECT observation_id,turn_id,raw_text,raw_payload_json "
                    "FROM observations WHERE episode_id=?", (episode_id,))}
                tombstones = {row[0] for row in connection.execute(
                    "SELECT target_id FROM tombstones WHERE target_type='observation'")}
        missing = [turn["turn_id"] for turn in delta if turn["turn_id"] not in found]
        empty = [turn["turn_id"] for turn in delta if turn.get("text") and
                 turn["turn_id"] in found and not (
                     found[turn["turn_id"]][2] or found[turn["turn_id"]][3]) and
                 found[turn["turn_id"]][0] not in tombstones]
        if missing or empty:
            raise ValidationFailure(
                f"actual incoming delta not represented in observations: missing={missing}, empty={empty}",
                stage="runtime_derivation", table="observations",
                row_difference={"missing_turn_ids": missing, "empty_turn_ids": empty},
                origin="runtime_derivation",
            )

    def _migrate_to(self, blob, old, target):
        versions = self.journal.get("versions")
        chain, cursor = [], target
        while cursor != old:
            if not cursor or cursor not in versions:
                raise ValidationFailure("replay snapshot is not an ancestor")
            chain.append(versions[cursor]["migration_sql"])
            cursor = versions[cursor]["parent"]
        for sql in reversed(chain):
            blob = migrate_blob(blob, sql)
        return blob

    async def _rebuilt_image(self, workspace, parent, sql, episode_id, position, key):
        """Re-derive an episode image from its visible turns under one workspace's code."""
        episode = self.dataset.episodes_by_id[episode_id]
        header = episode_header(episode)
        empty, _ = await self.operation(
            key + "/empty", partial(self.backend.memory, workspace, None, header, delta=[]))
        empty = migrate_blob(self._migrate_to(empty, "version_0001", parent), sql)
        turns = strip_hidden_fields(episode["turns"][:position + 1])
        rebuilt, _ = await self.operation(
            key + "/ingest", partial(self.backend.memory, workspace, empty, header, delta=turns))
        return rebuilt

    @staticmethod
    def _preserve_rebuilt(before, after):
        with tempfile.TemporaryDirectory() as tmp:
            states = []
            for name, blob in (("before", before), ("after", after)):
                path = Path(tmp) / name
                path.write_bytes(blob)
                with sqlite3.connect(path) as connection:
                    states.append((
                        sorted(connection.execute(
                            "SELECT episode_id,turn_id,raw_text,content_hash FROM observations")),
                        {tuple(row) for row in connection.execute(
                            "SELECT target_id,target_type FROM tombstones")},
                    ))
        (old_obs, old_tombs), (new_obs, new_tombs) = states
        if old_tombs - new_tombs:
            raise ValidationFailure("rebuilt image lost tombstones", stage="data_preservation",
                                    table="tombstones",
                                    row_difference={"lost": sorted(old_tombs - new_tombs)[:5]},
                                    origin="reingest")
        if old_obs != new_obs:
            raise ValidationFailure("rebuilt image changed raw observations",
                                    stage="data_preservation", table="observations",
                                    origin="reingest")

    @staticmethod
    def _preserve_images(before, after):
        with tempfile.TemporaryDirectory() as tmp:
            states = []
            for name, blob in (("before", before), ("after", after)):
                path = Path(tmp) / name
                path.write_bytes(blob)
                with sqlite3.connect(path) as connection:
                    states.append(dump(connection))
            preserve(*states, origin="reopening")

    # Summaries and materialized artifacts.
    def summary(self):
        rows = self.journal.official()
        pending = self.journal.get("pending_repairs", [])
        attempts = [dict(row) for row in self.journal.db.execute("SELECT * FROM attempts")]
        reports = [{**row, **json.loads(row["value"])} for row in attempts]
        repairs = [json.loads(row[0]) for row in self.journal.db.execute(
            "SELECT value FROM repairs ORDER BY boundary,attempt,id")]
        operations = [dict(row) for row in self.journal.db.execute(
            "SELECT * FROM operations ORDER BY key")]
        by_boundary = {}
        for operation in operations:
            bucket = by_boundary.setdefault(
                operation["key"].split("/")[0],
                {"tokens": 0, "seconds": 0.0, "transport_retries": 0, "cost_usd": None},
            )
            bucket["tokens"] += operation["tokens"]
            bucket["seconds"] += operation["seconds"]
            bucket["transport_retries"] += operation["retries"]
        incidents = self.journal.get("repair_incidents", [])
        return {
            "status": self.journal.get("status"),
            "event_boundary_index": self.journal.get("event_boundary_index"),
            "committed_system_version": self.committed,
            "candidate_system_version": self.journal.get("candidate_system_version"),
            "active_repair_incident": self.journal.get("active_repair_incident"),
            "scoreboard": cumulative(rows),
            "adaptation": {
                "repair_incidents": len([item for item in incidents if item.get("incident_id")]),
                "candidate_promotions": sum(row["status"] == "promoted" for row in attempts),
                "candidate_rejections": sum(row["status"] == "rejected" for row in attempts),
                "interrupted_candidates": sum(row["status"] == "interrupted" for row in attempts),
                "unresolved_repair_incidents": sum(
                    bool(item.get("incident_id") and not item.get("resolved")) for item in incidents),
                "pending_repairs": len([item for item in pending if not item.get("resolved")
                                        and not item.get("abandoned")]),
                "unresolved_failures": sorted(item["checkpoint_id"] for item in pending
                                              if not item.get("resolved")),
                "abandoned_failures": sorted(item["checkpoint_id"] for item in pending
                                             if item.get("abandoned")),
                "equivalent_attempt_rejections": sum(
                    report.get("rejection_reason") == "equivalent_previous_attempt"
                    for report in reports),
                "no_improvement_rejections": sum(
                    report.get("rejection_reason") == "no_measurable_improvement"
                    for report in reports),
                "safety_rejections": sum(
                    report.get("rejection_reason") == "safety_regression"
                    for report in reports),
                "per_boundary_usage": by_boundary,
            },
            "repair_prediction_count": len(repairs),
            "total_tokens": sum(operation["tokens"] for operation in operations),
            "elapsed_s": time.time() - self.journal.get("manifest")["started_at"],
            "pending_repair_state": pending,
            "cost_usd": None,
            "cost_note": "Dollar costs unavailable from provider usage; token counts are observational only.",
        }

    def export(self):
        write_json(self.run_dir / "run_manifest.json", self.journal.get("manifest"))
        write_json(self.run_dir / "event_schedule.json", {"policy": POLICY, "events": self.schedule})
        for attempt in self.journal.db.execute("SELECT * FROM attempts"):
            report = {**json.loads(attempt["value"]), "status": attempt["status"]}
            folder = (self.run_dir / "events" / f"boundary_{attempt['boundary']:04d}" /
                      "attempts" / f"attempt_{attempt['idx']:02d}")
            write_json(folder / "validation.json", report)
        active = self.journal.get("active_repair_incident")
        if active:
            write_json(
                self.run_dir / "events" / f"boundary_{active['boundary_index']:04d}" /
                "repair_incident.json", active)
        rows = self.journal.official()
        repairs = [json.loads(row[0]) for row in self.journal.db.execute(
            "SELECT value FROM repairs ORDER BY boundary,attempt,id")]
        promoted = {(row[0], row[1]) for row in self.journal.db.execute(
            "SELECT boundary,idx FROM attempts WHERE status='promoted'")}
        for row in rows:
            row["output"] = row["prediction"]["output"]
            related = [repair for repair in repairs
                       if repair["checkpoint_id"] == row["checkpoint_id"]]
            row["repair_attempts"] = [{
                "boundary": repair["repair_boundary_index"],
                "attempt": repair["repair_attempt_index"],
                "stage": repair.get("replay_stage"),
                "passed": repair["score"]["passed"],
            } for repair in related]
            row["repair_eventually_successful"] = any(
                repair["score"]["passed"] and
                (repair["repair_boundary_index"], repair["repair_attempt_index"]) in promoted
                for repair in related)
            row["first_attempt_action"] = row["prediction"]["output"].get("action")
            row["first_attempt_passed"] = row["score"]["passed"] if row["score"] else None
        for name, values in (("official_predictions.jsonl", rows),
                             ("repair_predictions.jsonl", repairs)):
            path = self.run_dir / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text("".join(encode(row) + "\n" for row in values))
            temporary.replace(path)
        for row in self.journal.db.execute("SELECT * FROM events"):
            folder = self.run_dir / "events" / f"boundary_{row['idx']:04d}"
            state = json.loads(row["value"])
            judged = [item for item in rows
                      if item["event_boundary_index"] == row["idx"] and item["score"]]
            if judged:
                write_json(folder / "official_judge_report.json", judged)
            if state.get("critique"):
                (folder / "critique.md").write_text(state["critique"]["text"] + "\n")
        write_json(self.run_dir / "cumulative_scoreboard.json", self.summary())

    def close(self):
        self.journal.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()
