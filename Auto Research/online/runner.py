"""Durable online adaptation with immutable official predictions.

Ingestion commits an episode image and cursor together. Promotion commits
migrated images, the version pointer, and any staged ingestion together. A
boundary has separate representation and answer-repair milestones, sharing one
durable failed-candidate counter.
"""
from __future__ import annotations

import asyncio
import contextvars
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
from online.backend import (
    Backend,
    DeveloperApproachUnworkable,
    InfrastructureFailure,
    PromptContextFailure,
)
from online.diagnostics import (
    build_incident_dossier,
    compact_incident_dossier,
    replay_behavior,
)
from online.failure_feedback import (
    compact_failure_feedback,
    sanitize_build_feedback,
    sanitized_failure_feedback,
)
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

_REPAIR_CLASSIFICATIONS = frozenset({
    "INGESTION", "AUTHORIZATION", "RETRIEVAL", "FACT_SELECTION", "RENDERING",
    "INFRASTRUCTURE", "ESCALATE",
})
_MAX_PLANNER_ATTEMPTS = 4


@dataclass(frozen=True)
class OnlineOptions:
    """Online validation, retry, and finite per-boundary candidate policy."""

    disable_redesign: bool = False
    replay_window: int = config.ONLINE_REPLAY_WINDOW
    utility_tolerance: float = config.ONLINE_UTILITY_TOLERANCE
    stream_order: str = POLICY
    max_infra_retries: int = config.MAX_INFRA_RETRIES
    max_failed_candidates: int = config.ONLINE_MAX_FAILED_CANDIDATES
    max_invalid_proposals: int = config.ONLINE_MAX_INVALID_PROPOSALS
    max_correction_rounds: int = config.ONLINE_MAX_CORRECTION_ROUNDS
    # Opt-in: rebuild replay and promotion images from the episode's turns under
    # each side's code, so ingestion-time repairs become measurable.
    replay_reingest: bool = False
    # Opt-in: predict and judge each replay side this many times; pass/fail is
    # the majority, so one noisy judge call cannot make or break a candidate.
    replay_votes: int = 1
    renderer_mode: str = config.ONLINE_RENDERER_MODE
    single_request_timeout_s: float = config.ONLINE_SINGLE_REQUEST_TIMEOUT_S
    developer_episode_timeout_s: float = config.ONLINE_DEVELOPER_EPISODE_TIMEOUT_S
    candidate_timeout_s: float = config.ONLINE_CANDIDATE_TIMEOUT_S
    incident_timeout_s: float = config.ONLINE_INCIDENT_TIMEOUT_S
    replay_vote_timeout_s: float = config.ONLINE_REPLAY_VOTE_TIMEOUT_S
    replay_concurrency: int = config.ONLINE_REPLAY_CONCURRENCY

    def __post_init__(self):
        if self.replay_window < 1 or self.max_infra_retries < 0:
            raise ValueError("invalid online replay/transport policy")
        if min(self.max_failed_candidates, self.max_invalid_proposals,
               self.max_correction_rounds) < 1:
            raise ValueError("online attempt limits must be at least one")
        if self.replay_votes < 1 or self.replay_votes % 2 == 0:
            raise ValueError("replay_votes must be a positive odd number")
        if not 0 <= self.utility_tolerance <= 1:
            raise ValueError("online utility tolerance must be between zero and one")
        if self.renderer_mode not in {"locked", "evolvable"}:
            raise ValueError("renderer_mode must be locked or evolvable")
        if min(self.single_request_timeout_s, self.developer_episode_timeout_s,
               self.candidate_timeout_s, self.incident_timeout_s,
               self.replay_vote_timeout_s) <= 0 or self.replay_concurrency < 1:
            raise ValueError("online liveness limits must be positive")


class BudgetExhausted(RuntimeError):
    """A finite candidate or incident wall-clock deadline was exhausted."""


class RepairBlocked(RuntimeError):
    pass


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
    "max_stalled_attempts",
    "max_duplicate_proposals",
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
        self.backend.renderer_mode = self.options.renderer_mode
        self.backend.online_developer_timeout_s = self.options.developer_episode_timeout_s
        self._replay_semaphore = asyncio.Semaphore(self.options.replay_concurrency)
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
                if (legacy or existing.get("options") != asdict(self.options) or
                        int(existing.get("format_version", 1)) < 4):
                    existing = {
                        **existing,
                        "format_version": max(4, int(existing.get("format_version", 1))),
                        "options": asdict(self.options),
                        "legacy_online_limits_ignored": {
                            **existing.get("legacy_online_limits_ignored", {}), **legacy,
                        },
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
        self._operation_key = contextvars.ContextVar("online_operation_key", default="")
        self._candidate_deadline = None
        self._incident_deadline = None
        self.backend.charge = self._charge
        if resume:
            self._migrate_legacy_online_state()
            unfinished = self.journal.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE status IN ('planning','building')"
            ).fetchone()[0]
            if unfinished:
                self._interrupt_unfinished_candidates()
                active = self.journal.get("active_repair_incident")
                if active:
                    self._transition(
                        active, "architect", candidate_system_version=None,
                        resume_note="interrupted candidate marked non-promoted; incident resumes",
                    )
        self.export()

    def _migrate_legacy_online_state(self):
        """Add state introduced by the automatic-attempt policy in place."""
        versions = self.journal.get("versions", {})
        changed_versions = False
        for index, (version, manifest) in enumerate(sorted(versions.items()), 1):
            defaults = {
                "memory_version": f"memory_v{index:04d}",
                "renderer_version": config.FIXED_RENDERER_VERSION,
                "contract_version": config.FIXED_CONTRACT_VERSION,
                "renderer_mode": "locked",
            }
            for key, value in defaults.items():
                if key not in manifest:
                    manifest[key] = value
                    changed_versions = True
        if changed_versions:
            with self.journal.db:
                self.journal.put("versions", versions)
        schedule_cursors = self.journal.get("episode_schedule_cursors", None)
        ingestion_map = self.journal.get("episode_ingested_turn_ids", None)
        if schedule_cursors is not None and ingestion_map is not None:
            return
        next_boundary = int(self.journal.get("event_boundary_index", 1))
        schedule_cursors = {}
        for event in self.schedule:
            if event["event_boundary_index"] >= next_boundary:
                break
            schedule_cursors[event["episode_id"]] = event["turn_position"]
        ingestion_map = {}
        for row in self.journal.db.execute("SELECT id,cursor FROM episodes"):
            episode = self.dataset.episodes_by_id[row["id"]]
            ingestion_map[row["id"]] = [
                turn["turn_id"] for turn in episode["turns"][:row["cursor"] + 1]
            ]
        with self.journal.db:
            self.journal.put("episode_schedule_cursors", schedule_cursors)
            self.journal.put("episode_ingested_turn_ids", ingestion_map)

    @staticmethod
    def _resume_options_compatible(stored, requested):
        current = {key: value for key, value in stored.items()
                   if key not in _LEGACY_LIMIT_KEYS}
        # These options change replay meaning. Absence in an older manifest is
        # the historical disabled/one-vote behavior, never permission to adopt
        # a new value silently during resume.
        current.setdefault("replay_reingest", False)
        current.setdefault("replay_votes", 1)
        current.setdefault("renderer_mode", "locked")
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
            "memory_version": "memory_v0001",
            "renderer_version": config.FIXED_RENDERER_VERSION,
            "contract_version": config.FIXED_CONTRACT_VERSION,
            "renderer_mode": self.options.renderer_mode,
            "code": code_manifest(work),
            "parent": None,
            "migration_sql": "",
            "status": "candidate",
            "promoted_after_boundary": 0,
            "supported_turn_fields": ["turn_id", "timestamp", "speaker", "turn_kind", "text"],
        }
        run_manifest = {
            "format_version": 4,
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
            self.journal.put("escalation_queue", [])
            self.journal.put("versions", {version: manifest})
            self.journal.put("status", "ready")
            self.journal.put("cumulative_counts", cumulative([]))
            self.journal.put("pending_repairs", [])
            self.journal.put("episode_schedule_cursors", {})
            self.journal.put("episode_ingested_turn_ids", {})
        write_json(work.parent / "manifest.json", manifest)
        write_json(work.parent / "parent.json", {"parent": None})
        write_json(work.parent / "promotion_report.json", {"baseline": True})
        write_json(self.run_dir / "event_schedule.json", {"policy": POLICY, "events": self.schedule})
        write_json(self.run_dir / "run_manifest.json", run_manifest)

    # Observable operations. Tokens and time are never stop conditions.
    def _charge(self, usage):
        tokens = int((usage or {}).get("total_tokens") or 0)
        operation_key = self._operation_key.get() or self.current_operation
        with self.journal.db:
            self.journal.db.execute(
                "INSERT OR IGNORE INTO operations(key) VALUES (?)", (operation_key,))
            self.journal.db.execute(
                "UPDATE operations SET tokens=tokens+? WHERE key=?",
                (tokens, operation_key),
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
            now = time.monotonic()
            if self._incident_deadline is not None and now >= self._incident_deadline:
                raise BudgetExhausted("online incident wall-clock limit exhausted")
            if self._candidate_deadline is not None and now >= self._candidate_deadline:
                raise BudgetExhausted("online candidate wall-clock limit exhausted")
            context_token = self._operation_key.set(key)
            started = time.monotonic()
            with self.journal.db:
                self.journal.db.execute("INSERT OR IGNORE INTO operations(key) VALUES (?)", (key,))
            try:
                operation_timeout = (self.options.developer_episode_timeout_s
                                     if "/developer" in key else
                                     self.options.replay_vote_timeout_s
                                     if "_replay/" in key else
                                     self.options.single_request_timeout_s)
                budget_deadlines = [
                    (name, deadline) for name, deadline in (
                        ("incident", self._incident_deadline),
                        ("candidate", self._candidate_deadline),
                    ) if deadline is not None
                ]
                limiting_budget = min(budget_deadlines, key=lambda item: item[1]) \
                    if budget_deadlines else None
                budget_remaining = (
                    limiting_budget[1] - now if limiting_budget is not None else None)
                budget_is_limit = (
                    budget_remaining is not None and budget_remaining <= operation_timeout)
                timeout = max(0.001, min(
                    operation_timeout,
                    budget_remaining if budget_remaining is not None else operation_timeout,
                ))
                task = asyncio.create_task(call())
                deadline = time.monotonic() + timeout
                runner_timed_out = False
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        runner_timed_out = True
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise asyncio.TimeoutError
                    done, _ = await asyncio.wait({task}, timeout=min(60.0, remaining))
                    if done:
                        return task.result()
                    log.info("online heartbeat operation=%s elapsed_s=%.1f",
                             key, time.monotonic() - started)
            except asyncio.TimeoutError as exc:
                if runner_timed_out and budget_is_limit:
                    raise BudgetExhausted(
                        f"online {limiting_budget[0]} wall-clock limit exhausted during {key}"
                    ) from exc
                raise InfrastructureFailure(
                    f"{key}: bounded operation timed out after {operation_timeout:.1f}s") from exc
            except PromptContextFailure:
                # A deterministic request-size error will be identical on every
                # retry. Preserve the boundary for an operator/code fix without
                # spending the transport retry allowance.
                raise
            except (InfrastructureFailure, ConnectionError, TimeoutError) as exc:
                with self.journal.db:
                    self.journal.db.execute(
                        "UPDATE operations SET retries=retries+1 WHERE key=?", (key,))
                retries = self.journal.db.execute(
                    "SELECT retries FROM operations WHERE key=?", (key,)).fetchone()[0]
                if retries > self.options.max_infra_retries:
                    raise InfrastructureFailure(
                        f"{key}: transport retry limit reached; boundary remains blocked; "
                        f"last transport error: {exc}") from exc
                log.warning("Retrying same online operation %s after transport failure (%d)",
                            key, retries)
                await asyncio.sleep(min(0.1 * 2 ** (retries - 1), 2))
            finally:
                with self.journal.db:
                    self.journal.db.execute(
                        "UPDATE operations SET seconds=seconds+? WHERE key=?",
                        (time.monotonic() - started, key),
                    )
                self._operation_key.reset(context_token)

    # Visible causal context and durable incidents.
    def _visible_outcome(self, row):
        output = row["prediction"].get("output", {})
        return {
            "checkpoint_id": row["checkpoint_id"],
            "event_boundary_index": row["event_boundary_index"],
            "requested_action_type": "retrieve",
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
            if (not incident.get("resolved") and
                    incident.get("phase") not in {"infrastructure_blocked", "prompt_blocked"}):
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
            "SELECT boundary,idx,value FROM attempts "
            "WHERE status IN ('planning','building') ORDER BY boundary,idx"))
        for row in rows:
            report = json.loads(row["value"])
            candidate_created = bool(report.get("candidate_system_version"))
            report.update({
                "failed_gate": "interrupted",
                "rejection_reason": (
                    "interrupted_candidate" if candidate_created else "interrupted_planning"),
                "reasons": [
                    "interrupted candidate was never promoted" if candidate_created else
                    "interrupted Architect call created no candidate"
                ],
                "phase": "interrupted" if candidate_created else "planner_interrupted",
                "counts_as_failed_candidate": False,
                "counts_as_candidate_rejection": False,
            })
            status = "interrupted" if candidate_created else "planner_interrupted"
            self._finish_attempt(row["boundary"], row["idx"], status, report)
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
                "error_present": bool(ingestion_error),
                "error_signature": hashlib.sha256(
                    str(ingestion_error or "").encode()).hexdigest()[:16],
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
            # Old journals predate the automatic attempt policy. Missing
            # counters mean no failed candidate has yet been durably charged.
            if ("failed_candidate_attempts" not in active or
                    "invalid_proposal_count" not in active or
                    "planner_attempt_count" not in active or
                    "representation_committed" not in active):
                event_state = self.journal.event(index)[1]
                active = {
                    **active,
                    "failed_candidate_attempts": int(
                        active.get("failed_candidate_attempts") or
                        active.get("stalled_rejections") or 0),
                    "invalid_proposal_count": int(
                        active.get("invalid_proposal_count") or 0),
                    "planner_attempt_count": int(
                        active.get("planner_attempt_count") or 0),
                    "planner_abstention_history": list(
                        active.get("planner_abstention_history") or []),
                    "representation_committed": bool(
                        active.get("representation_committed") or
                        (active.get("trigger_type") == "scored_checkpoint_failure" and
                         event_state.get("frozen_version"))),
                    "representation_committed_version": active.get(
                        "representation_committed_version") or (
                            event_state.get("representation_committed_version") or
                            event_state.get("frozen_version")
                            if active.get("trigger_type") == "scored_checkpoint_failure"
                            else None),
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
        event_state = self.journal.event(index)[1]
        representation_committed = trigger_type == "scored_checkpoint_failure"
        failed_candidate_attempts = (
            int(event_state.get("failed_candidate_attempts") or 0)
            if representation_committed else 0)
        representation_version = (
            event_state.get("representation_committed_version") or
            event_state.get("frozen_version")
            if representation_committed else None)
        incident = {
            "incident_id": incident_id,
            "boundary_index": index,
            "trigger_type": trigger_type,
            "repair_objective": (
                "represent and atomically ingest the exact staged input"
                if trigger_type == "pre_ingestion" else
                "improve one or more unresolved checkpoints without regression"
            ),
            "safe_parent_version": parent,
            "target_checkpoint_ids": targets,
            "unresolved_target_checkpoint_ids": targets,
            "staged_input": staged_input,
            "staged_input_digest": (
                digest(staged_input) if staged_input is not None else
                event_state.get("staged_input_digest") or event_state.get("delta_digest")),
            "ingestion_error": ingestion_error,
            "structural_summary": summary,
            "candidate_attempt_history": prior_attempts,
            "structured_feedback_history": [],
            "diagnosis_review_history": [],
            "architect_incident_dossier": dossier,
            "promotion_history": [],
            "failed_candidate_attempts": failed_candidate_attempts,
            "invalid_proposal_count": 0,
            "invalid_proposal_history": [],
            "planner_attempt_count": 0,
            "planner_abstention_history": [],
            "representation_committed": representation_committed,
            "representation_committed_version": representation_version,
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
        dossier = compact_incident_dossier(incident.get("architect_incident_dossier"))
        likely = {str(item.get("likely_problem") or "") for item in dossier.get("targets", [])}
        allowed_component = (
            "renderer" if incident["trigger_type"] != "pre_ingestion" and
            likely == {"RENDERING"} else "memory")
        context = {
            "renderer_mode": self.options.renderer_mode,
            "allowed_repair_component": allowed_component,
            "ownership_contract": {
                "memory": "authorization, retrieval, selected/required claims, fallback",
                "renderer": "proposed wording only",
                "fixed_contract": "trusted release approval; never candidate-editable",
            },
            "incident_report": {
                "incident_id": incident["incident_id"],
                "boundary_index": incident["boundary_index"],
                "trigger_type": incident["trigger_type"],
                "repair_objective": incident["repair_objective"],
                "safe_parent_version": incident["safe_parent_version"],
                "structural_summary": incident.get("structural_summary"),
                "ingestion_error_present": bool(incident.get("ingestion_error")),
                "ingestion_error_signature": hashlib.sha256(
                    str(incident.get("ingestion_error") or "").encode()).hexdigest()[:16],
                "exact_staged_input_digest": incident.get("staged_input_digest"),
                "unresolved_targets": [self._visible_outcome(row) for row in official
                                       if row["checkpoint_id"] in targets],
                "previous_approaches": incident.get("candidate_attempt_history", []),
                "replay_reingest": self.options.replay_reingest,
            },
            "architect_incident_dossier": dossier,
            "latest_complete_structured_feedback": compact_failure_feedback(
                previous[0] if previous else None),
            "earlier_attempt_summaries": [self._compact_feedback(item) for item in earlier],
        }
        assert_no_hidden_fields(context, where="online Architect incident context")
        return context

    @staticmethod
    def _compact_feedback(feedback):
        return {key: feedback.get(key) for key in (
            "incident_id", "safe_parent_version", "failure_category", "pipeline_phase",
            "rejection_code", "rejection_reason", "improved_targets", "remaining_targets",
            "regressed_targets",
            "diagnosis_signature", "design_signature",
            "source_diff_signature", "behavioral_signature",
        )}

    # Main prequential state machine.
    async def run(self, max_boundaries=None):
        processed = 0
        try:
            status = self.journal.get("status")
            if status in {"infrastructure_blocked", "prompt_blocked"}:
                if not self.retry_blocked:
                    failure = (PromptContextFailure if status == "prompt_blocked"
                               else InfrastructureFailure)
                    raise failure(self.journal.get("halt_reason"))
                incident = self.journal.get("active_repair_incident")
                if incident:
                    incident = self._transition(
                        incident, "architect", infrastructure_error=None, prompt_error=None,
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
                        incident, "architect",
                        failed_candidate_attempts=int(
                            incident.get("failed_candidate_attempts") or
                            incident.get("stalled_rejections") or 0),
                        invalid_proposal_count=0,
                        stalled_rejections=0, duplicate_rejections=0,
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
                terminal_phase = self.journal.event(index)[0]
                log.info("Online boundary %d/%d outcome=%s committed_version=%s",
                         index, len(self.schedule), terminal_phase, self.committed)
            with self.journal.db:
                self.journal.put(
                    "status",
                    "complete" if self.journal.get("event_boundary_index") > len(self.schedule)
                    else "paused",
                )
        except InfrastructureFailure as exc:
            prompt_failure = isinstance(exc, PromptContextFailure)
            blocked_phase = "prompt_blocked" if prompt_failure else "infrastructure_blocked"
            incident = self.journal.get("active_repair_incident")
            if incident:
                self._transition(
                    incident, blocked_phase,
                    infrastructure_error=None if prompt_failure else str(exc),
                    prompt_error=str(exc) if prompt_failure else None,
                )
            with self.journal.db:
                self.journal.put("status", blocked_phase)
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
            schedule_cursors = self.journal.get("episode_schedule_cursors", {})
            schedule_cursor = int(schedule_cursors.get(episode_id, -1))
            if event["turn_position"] <= schedule_cursor:
                raise RuntimeError("episode schedule cursor would move backwards")
            delta = strip_hidden_fields(
                episode["turns"][schedule_cursor + 1:event["turn_position"] + 1])
            summary = structural_summary(delta)
            check = compatibility(summary, self.version_manifest())
            write_json(folder / "input_delta.json", {
                "episode_id": episode_id,
                "previous_cursor": cursor,  # legacy alias for ingestion cursor
                "previous_ingestion_cursor": cursor,
                "previous_schedule_cursor": schedule_cursor,
                "turns": delta,
            })
            write_json(folder / "compatibility.json", check)
            state = {
                "compatibility": check,
                "summary": summary,
                "staged_delta": delta,
                "delta_digest": digest(delta),
                "previous_schedule_cursor": schedule_cursor,
                "ingested_turn_ids": [turn["turn_id"] for turn in delta],
            }
            with self.journal.db:
                self.journal.save_event(index, "staged", state)
            phase = "staged"

        if phase == "staged":
            row = self.journal.db.execute(
                "SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            cursor = row["cursor"] if row else -1
            schedule_cursors = self.journal.get("episode_schedule_cursors", {})
            schedule_cursor = int(schedule_cursors.get(episode_id, -1))
            fresh_delta = strip_hidden_fields(
                episode["turns"][schedule_cursor + 1:event["turn_position"] + 1])
            delta = state.get("staged_delta", fresh_delta)
            if (digest(delta) != digest(fresh_delta) or
                    state.get("delta_digest") not in {None, digest(delta)} or
                    state.get("previous_schedule_cursor", schedule_cursor) != schedule_cursor):
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
                if phase in {"unrepresented_after_5_attempts", "escalated"}:
                    return
                if phase != "ingested":
                    raise RepairBlocked("staged ingestion incident did not commit")
            else:
                state = {
                    **state,
                    "frozen_version": version,
                    "representation_committed": True,
                    "representation_committed_version": version,
                    "failed_candidate_attempts": int(
                        state.get("failed_candidate_attempts") or 0),
                    "invalid_proposal_count": int(
                        state.get("invalid_proposal_count") or 0),
                    "staged_input_digest": state.get("delta_digest"),
                    "ingestion_occurred": True,
                }
                with self.journal.db:
                    self.journal.db.execute(
                        "INSERT OR REPLACE INTO episodes VALUES (?, ?, ?)",
                        (episode_id, event["turn_position"], snapshot),
                    )
                    ingested = self.journal.get("episode_ingested_turn_ids", {})
                    ingested[episode_id] = list(dict.fromkeys(
                        list(ingested.get(episode_id, [])) + state["ingested_turn_ids"]))
                    self.journal.put("episode_ingested_turn_ids", ingested)
                    self.journal.save_event(index, "ingested", state, snapshot)
                phase = "ingested"

        if phase in {"unrepresented_after_5_attempts", "escalated"}:
            return

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
                          if not item.get("resolved") and not item.get("terminal_outcome")]
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
                terminal_phase, _, _ = self.journal.event(index)
                if terminal_phase in {
                    "unrepaired_after_5_attempts",
                    "partially_repaired_with_remaining_failures",
                    "escalated",
                }:
                    return
                unresolved = [item for item in self.journal.get("pending_repairs", [])
                              if not item.get("resolved") and not item.get("terminal_outcome")]
                if unresolved:
                    raise RepairBlocked("active scored incident returned with unresolved targets")
                state["repair_disposition"] = "resolved"
            outcome = ("repaired_and_passed" if state.get("repair_disposition") or
                       state.get("repair_occurred") else "passed")
            boundary_failed = max([
                int(item.get("failed_candidate_attempts") or 0)
                for item in self.journal.get("repair_incidents", [])
                if item.get("boundary_index") == index
            ] or [0])
            boundary_invalid = max([
                int(item.get("invalid_proposal_count") or 0)
                for item in self.journal.get("repair_incidents", [])
                if item.get("boundary_index") == index
            ] or [0])
            with self.journal.db:
                schedule_cursors = self.journal.get("episode_schedule_cursors", {})
                schedule_cursors[episode_id] = event["turn_position"]
                self.journal.put("episode_schedule_cursors", schedule_cursors)
                self.journal.save_event(index, outcome, {
                    **state, "boundary_outcome": outcome,
                    "failed_candidate_attempts": boundary_failed,
                    "invalid_proposal_count": boundary_invalid,
                    "committed_system_version": self.committed,
                    "schedule_advanced": True, "ingestion_occurred": True,
                    "representation_committed": True,
                    "representation_committed_version": state.get(
                        "representation_committed_version", state.get("frozen_version")),
                    "staged_input_digest": state.get(
                        "staged_input_digest", state.get("delta_digest")),
                }, snapshot)
                self.journal.put("event_boundary_index", index + 1)
                self.journal.put("repair_attempt_index", 0)
            write_json(folder / "boundary_outcome.json", {
                "boundary_index": index,
                "boundary_outcome": outcome,
                "failed_candidate_attempts": boundary_failed,
                "invalid_proposal_count": boundary_invalid,
                "schedule_advanced": True,
                "ingestion_occurred": True,
                "committed_system_version": self.committed,
                "representation_committed": True,
                "representation_committed_version": state.get(
                    "representation_committed_version", state.get("frozen_version")),
                "staged_input_digest": state.get(
                    "staged_input_digest", state.get("delta_digest")),
            })

    # Focused repair loop.
    def _next_attempt_index(self, boundary):
        row = self.journal.db.execute(
            "SELECT COALESCE(MAX(idx),0) FROM attempts WHERE boundary=?", (boundary,)).fetchone()
        return int(row[0]) + 1

    @staticmethod
    def _normalize(text):
        return " ".join(str(text or "").lower().split())

    @classmethod
    def _normalized_repair_intent(cls, design):
        """Canonical causal intent; prose style and prompt position are ignored."""
        scope = design.get("repair_scope") if isinstance(design.get("repair_scope"), dict) else {}
        prediction = (design.get("predicted_replay_change")
                      if isinstance(design.get("predicted_replay_change"), dict) else {})
        work = " ".join(str(item) for item in design.get("work_order") or [])
        mechanism_text = cls._normalize(
            design.get("failure_mechanism") or design.get("specific_hypothesis") or work)
        synonyms = {
            "choose": "select", "selected": "select", "selection": "select",
            "filtering": "filter", "filtered": "filter", "screen": "filter",
            "ranking": "rank", "scoring": "rank", "prioritize": "rank",
            "validation": "validate", "validator": "validate", "verify": "validate",
            "parsing": "parse", "parser": "parse", "decode": "parse",
            "wording": "prompt", "instruction": "prompt", "instructions": "prompt",
            "memoize": "cache", "reuse": "cache", "cached": "cache",
        }
        tokens = [synonyms.get(token.strip(".,:;()[]{}"), token.strip(".,:;()[]{}"))
                  for token in mechanism_text.split()]
        mechanism_markers = sorted(set(tokens) & {
            "select", "filter", "rank", "validate", "parse", "prompt", "cache",
            "schema", "contract", "fallback", "authorize", "permission", "lifecycle",
            "provenance", "atomic", "role", "timeout", "concurrent", "migration",
        })
        change_type = str(design.get("change_type") or "").strip().lower()
        if not change_type:
            change_type = (
                "prompt_edit" if "prompt" in mechanism_markers else
                "schema_change" if "schema" in mechanism_markers else
                "migration" if design.get("migration_sql") else
                "code_change")
        event = str(design.get("cited_causal_event") or
                    design.get("addressed_decision_rule") or "").strip().lower()
        observable = str(prediction.get("observable") or "").strip().lower()
        return {
            "repair_component": str(design.get("repair_component") or "").lower(),
            "failure_stage": str(design.get("failure_classification") or "").upper(),
            "failure_mechanism": mechanism_markers,
            "target_file": str(scope.get("file") or "").lower(),
            "target_function_or_artifact": str(scope.get("function") or "").lower(),
            "change_type": change_type,
            "migration_mechanism": (
                hashlib.sha256(cls._normalize(design.get("migration_sql")).encode()).hexdigest()
                if str(design.get("migration_sql") or "").strip() else None),
            "cited_causal_event_or_contract_reason": event,
            "expected_observable": observable,
        }

    @staticmethod
    def _validate_focused_design(design, *, require_targets=False):
        if not isinstance(design, dict):
            raise ValidationFailure("Architect proposal is not an object", stage="architect")
        classification = str(design.get("failure_classification") or "")
        if classification not in _REPAIR_CLASSIFICATIONS - {"ESCALATE"}:
            raise ValidationFailure(
                "Architect failure_classification must be exactly one of INGESTION, "
                "AUTHORIZATION, RETRIEVAL, FACT_SELECTION, or RENDERING for an actionable repair",
                stage="architect",
            )
        if classification == "INFRASTRUCTURE":
            raise ValidationFailure(
                "INFRASTRUCTURE failures must be escalated without a product candidate",
                stage="architect")
        component = str(design.get("repair_component") or "")
        if component not in {"memory", "renderer"}:
            raise ValidationFailure(
                "Architect repair_component must be exactly memory or renderer",
                stage="architect")
        expected_component = "renderer" if classification == "RENDERING" else "memory"
        if component != expected_component:
            raise ValidationFailure(
                f"{classification} is owned by {expected_component}, not {component}",
                stage="diagnosis_review")
        if design.get("decision") not in {"DATA_MIGRATION", "CODE_OR_SCHEMA_CHANGE"}:
            raise ValidationFailure(
                "Architect must propose one actionable repair for an unresolved incident",
                stage="architect",
            )
        diagnosis = str(design.get("diagnosis") or "").strip()
        hypothesis = str(design.get("specific_hypothesis") or "").strip()
        goal = str(design.get("focused_goal") or "").strip()
        work_order = design.get("work_order")
        scope = design.get("repair_scope")
        safety = design.get("safety_preservation_checks")
        if (not diagnosis or not hypothesis or not goal or
                not isinstance(work_order, list) or len(work_order) != 1):
            raise ValidationFailure(
                "Architect proposal must contain one diagnosis, one specific_hypothesis, "
                "one focused_goal, and exactly one work_order item",
                stage="architect",
            )
        if any(not str(item).strip() for item in work_order):
            raise ValidationFailure("Architect work order is empty", stage="architect")
        if (not isinstance(scope, dict) or any(
                not str(scope.get(key) or "").strip()
                for key in ("subsystem", "file", "function"))):
            raise ValidationFailure(
                "Architect repair_scope must name subsystem, file, and function",
                stage="architect",
            )
        target_file = str(scope.get("file") or "")
        allowed_prefix = "renderer/" if component == "renderer" else "memory_system/"
        if not target_file.startswith(allowed_prefix):
            raise ValidationFailure(
                f"{component} repair must target an editable {allowed_prefix} artifact",
                stage="diagnosis_review")
        if target_file.endswith("renderer_contract.py") or target_file == "renderer_contract.py":
            raise ValidationFailure("fixed renderer contract is not candidate-editable",
                                    stage="diagnosis_review")
        if not isinstance(safety, list) or not safety or any(
                not str(item).strip() for item in safety):
            raise ValidationFailure(
                "Architect proposal must include safety_preservation_checks",
                stage="architect",
            )
        targets = design.get("intended_target_ids")
        if require_targets and (not isinstance(targets, list) or not targets or any(
                not str(item).strip() for item in targets)):
            raise ValidationFailure(
                "Architect proposal must explicitly name target checkpoint IDs",
                stage="architect",
            )

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

    @staticmethod
    def _dossier_trace_events(dossier):
        found = {}
        def walk(value):
            if isinstance(value, dict):
                if value.get("trace_id"):
                    found[str(value["trace_id"])] = {
                        "rule": str(value.get("decision_rule") or value.get("rule") or ""),
                        "stage": str(value.get("decision_stage") or value.get("stage") or ""),
                    }
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
        component = str(design.get("repair_component") or "")
        classification = str(design.get("failure_classification") or "")
        expected_component = "renderer" if classification == "RENDERING" else "memory"
        checks["component_owns_failed_behavior"] = component == expected_component
        if not checks["component_owns_failed_behavior"]:
            feedback.append(
                f"{classification} cannot be repaired by {component}; owner is {expected_component}")
        scope_file = str((design.get("repair_scope") or {}).get("file") or "")
        expected_prefix = "renderer/" if component == "renderer" else "memory_system/"
        checks["target_is_editable_for_component"] = scope_file.startswith(expected_prefix)
        if not checks["target_is_editable_for_component"]:
            feedback.append(f"repair target must be inside {expected_prefix}")
        checks["can_affect_final_observable"] = not (
            classification == "RENDERING" and component == "memory")
        if not checks["can_affect_final_observable"]:
            feedback.append("a rendering failure cannot be repaired by changing memory fallback wording")
        intended = {str(item) for item in (design.get("intended_target_ids") or
                                           incident.get("unresolved_target_checkpoint_ids") or [])}
        unresolved = set(incident.get("unresolved_target_checkpoint_ids") or [])
        checks["intended_targets_are_current"] = (
            incident.get("trigger_type") == "pre_ingestion" or
            (bool(intended) and intended <= unresolved))
        if not checks["intended_targets_are_current"]:
            feedback.append(
                "intended_target_ids must be a non-empty subset of current unresolved targets")
        trace_events = self._dossier_trace_events(incident.get("architect_incident_dossier"))
        trace_rules = {key: value["rule"] for key, value in trace_events.items()}
        citations = design.get("trace_citations")
        citations = citations if isinstance(citations, list) else []
        missing = sorted({str(item) for item in citations if str(item) not in trace_rules})
        checks["cited_traces_exist"] = bool(citations) and not missing
        if not citations:
            feedback.append("cite one or more trace_id values from architect_incident_dossier")
        elif missing:
            feedback.append("unknown trace citations: " + ", ".join(missing))

        allowed_trace_stages = {
            "INGESTION": {"ingestion", "state_reconstruction", "status_gate", "record_time_gate"},
            "AUTHORIZATION": {"authorization_gate", "projection_gate", "deletion_gate"},
            "RETRIEVAL": {"result_selection", "retrieval", "output_composition"},
            "FACT_SELECTION": {"result_selection", "answer_plan", "output_composition", "release"},
            "RENDERING": {"renderer_contract", "rendering", "output_composition", "release"},
        }
        cited_stages = {trace_events[str(item)]["stage"] for item in citations
                        if str(item) in trace_events}
        checks["cited_event_belongs_to_repair_stage"] = bool(
            cited_stages & allowed_trace_stages.get(classification, set()) or
            (incident.get("trigger_type") == "pre_ingestion" and
             classification == "INGESTION" and citations))
        if not checks["cited_event_belongs_to_repair_stage"]:
            feedback.append("cited event does not belong to the proposed failure stage")

        addressed = str(design.get("addressed_decision_rule") or "").strip()
        cited_rules = {trace_rules.get(str(item), "") for item in citations}
        checks["addresses_cited_decision_rule"] = bool(
            addressed and addressed in cited_rules)
        if not checks["addresses_cited_decision_rule"]:
            feedback.append(
                "addressed_decision_rule must exactly name a rule on a cited causal event")

        prediction = design.get("predicted_replay_change")
        observable = prediction.get("observable") if isinstance(prediction, dict) else None
        expected = prediction.get("expected_change") if isinstance(prediction, dict) else None
        allowed_observables = {
            "action", "rendered_answer", "authorized_evidence", "decision_trace", "pass_fail",
            "selected_claims", "required_claims", "contract_result", "fallback_usage",
            "answer_source",
        }
        checks["predicted_change_is_observable"] = (
            observable in allowed_observables and bool(str(expected or "").strip()))
        if not checks["predicted_change_is_observable"]:
            feedback.append(
                "predicted_replay_change needs an observable field and a concrete expected_change")

        observable_owners = {
            "action": {"memory"}, "authorized_evidence": {"memory"},
            "decision_trace": {"memory"}, "selected_claims": {"memory"},
            "required_claims": {"memory"}, "rendered_answer": {"memory", "renderer"},
            "pass_fail": {"memory", "renderer"}, "contract_result": {"renderer"},
            "fallback_usage": {"renderer"}, "answer_source": {"renderer"},
        }
        checks["target_can_affect_predicted_observable"] = (
            component in observable_owners.get(observable, set()) and
            bool(str((design.get("repair_scope") or {}).get("function") or "").strip()))
        if not checks["target_can_affect_predicted_observable"]:
            feedback.append("the selected component/file/function cannot affect that replay observable")

        duplicate = next((item for item in reversed(previous)
                          if item.get("db_attempt_index") != report["attempt_index"]
                          and item.get("repair_intent_signature") ==
                          report.get("repair_intent_signature")), None)
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
        self._incident_deadline = time.monotonic() + self.options.incident_timeout_s
        prefix = f"boundary_{index:04d}"
        if int(incident.get("failed_candidate_attempts") or 0) >= (
                self.options.max_failed_candidates):
            self._terminalize_failed_boundary(incident)
            return False
        while (not incident.get("resolved") and
               (incident.get("unresolved_target_checkpoint_ids") or
                incident["trigger_type"] == "pre_ingestion")):
            attempt_idx = self._next_attempt_index(index)
            candidate_number = int(incident.get("failed_candidate_attempts") or 0) + 1
            planner_attempt_index = int(incident.get("planner_attempt_count") or 0) + 1
            attempt_dir = self.run_dir / "events" / prefix / "attempts" / f"attempt_{attempt_idx:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            parent = incident["safe_parent_version"]
            report = {
                "incident_id": incident["incident_id"],
                "boundary_index": index,
                "trigger_type": incident["trigger_type"],
                "repair_objective": incident["repair_objective"],
                "attempt_index": attempt_idx,
                "candidate_number": None,
                "planner_attempt_index": planner_attempt_index,
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
                    "isolation", "no_checkpoint_specialization", "exact_staged_input",
                    "required_answers", "targeted_replay", "privacy", "deletion", "utility",
                    "grounding", "component_scope", "renderer_schema", "broad_validation",
                )},
                "targeted_replay": {"status": "not_run"},
                "broad_validation": {"status": "not_run"},
                "started_at": time.time(),
            }
            self._candidate_deadline = time.monotonic() + self.options.candidate_timeout_s
            with self.journal.db:
                self.journal.db.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?)",
                    (index, attempt_idx, "planning", encode(report)),
                )
                self.journal.put("repair_attempt_index", attempt_idx)
            incident = self._transition(
                incident, "architect", current_candidate_number=None,
                current_attempt_index=attempt_idx,
            )
            op = prefix + f"/attempt_{attempt_idx:02d}"
            try:
                raw_design = await self.operation(
                    op + "/architect",
                    partial(self.backend.architect, self._architect_context(incident),
                            self.workspace(parent)),
                )
                raw_design = Backend._normalize_architect_design(raw_design)
                hidden_design_error = None
                try:
                    assert_no_hidden_fields(raw_design, where="Architect proposal")
                except AssertionError as exc:
                    hidden_design_error = str(exc)
                design = strip_hidden_fields(raw_design)
                report["design"] = design
                if isinstance(design, dict) and (
                        design.get("decision") == "ESCALATE" or
                        design.get("failure_classification") == "ESCALATE"):
                    write_json(attempt_dir / "design.json", design)
                    report["diagnosis"] = design.get("diagnosis")
                    incident = self._record_planner_abstention(
                        incident, report, attempt_idx, "architect_escalated",
                        str(design.get("escalation_reason") or design.get("diagnosis") or
                            "visible evidence is insufficient for a safe proposal"),
                        force_escalate=True,
                    )
                    return False
                if isinstance(design, dict) and design.get("failure_classification") == "INFRASTRUCTURE":
                    write_json(attempt_dir / "design.json", design)
                    report["diagnosis"] = design.get("diagnosis")
                    self._record_planner_abstention(
                        incident, report, attempt_idx, "infrastructure_diagnosis",
                        "infrastructure failures are not product repair candidates",
                        force_escalate=True)
                    return False
                if (isinstance(design, dict) and
                        design.get("failure_classification") == "RENDERING" and
                        self.options.renderer_mode == "locked"):
                    write_json(attempt_dir / "design.json", design)
                    report["diagnosis"] = design.get("diagnosis")
                    self._record_planner_abstention(
                        incident, report, attempt_idx, "locked_renderer_escalation",
                        "true rendering failure is not repairable in benchmark-locked mode",
                        force_escalate=True)
                    return False
                if isinstance(design, dict) and design.get("decision") == "NO_VIABLE_REPAIR":
                    write_json(attempt_dir / "design.json", design)
                    report["diagnosis"] = design.get("diagnosis")
                    incident = self._record_planner_abstention(
                        incident, report, attempt_idx, "architect_no_viable_repair",
                        str(design.get("diagnosis") or "Architect found no viable focused repair"))
                    if incident.get("resolved"):
                        return False
                    continue
                report["diagnosis"] = design.get("diagnosis") if isinstance(design, dict) else None
                report["failure_classification"] = (
                    design.get("failure_classification") if isinstance(design, dict) else None)
                report["focused_goal"] = (
                    design.get("focused_goal") if isinstance(design, dict) else None)
                report["repair_component"] = (
                    design.get("repair_component") if isinstance(design, dict) else None)
                report["design_summary"] = (
                    design.get("work_order") if isinstance(design, dict) else None)
                report["intended_target_ids"] = (
                    list(design.get("intended_target_ids") or
                         incident.get("unresolved_target_checkpoint_ids") or [])
                    if isinstance(design, dict) else [])
                try:
                    if hidden_design_error:
                        raise ValidationFailure(hidden_design_error, stage="diagnosis_review")
                    self._validate_focused_design(
                        design,
                        require_targets=(
                            incident["trigger_type"] == "scored_checkpoint_failure"),
                    )
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
                    incident = self._record_planner_abstention(
                        incident, report, attempt_idx, "diagnosis_review_rejected", str(exc))
                    if incident.get("resolved"):
                        return False
                    continue
                report["diagnosis_signature"] = digest(self._normalize(design["diagnosis"]))
                report["design_signature"] = digest({
                    "focused_goal": self._normalize(design["focused_goal"]),
                    "work_order": [self._normalize(item) for item in design["work_order"]],
                })
                sql = str(design.get("migration_sql") or "")
                report["migration_signature"] = hashlib.sha256(
                    self._normalize(sql).encode()).hexdigest()
                report["normalized_repair_intent"] = self._normalized_repair_intent(design)
                report["repair_intent_signature"] = digest(
                    report["normalized_repair_intent"])
                write_json(attempt_dir / "design.json", design)
                previous = self._prior_reports(
                    incident, status={"rejected", "invalid_proposal", "planner_abstention"})
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
                    incident = self._record_planner_abstention(
                        incident, report, attempt_idx, review["rejection_reason"],
                        "; ".join(review["feedback"]),
                    )
                    if incident.get("resolved"):
                        return False
                    continue

                sequence = int(self.journal.get("version_sequence", 1)) + 1
                candidate = f"version_{sequence:04d}"
                report["candidate_number"] = candidate_number
                report["candidate_system_version"] = candidate
                with self.journal.db:
                    self.journal.put("version_sequence", sequence)
                    self.journal.put("candidate_system_version", candidate)
                work = attempt_dir / "workspace"
                copy_workspace(self.workspace(parent), work)
                incident = self._transition(
                    incident, "developing", candidate_system_version=candidate,
                    current_candidate_number=candidate_number)
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
                component = str(design.get("repair_component") or "")
                allowed_prefix = "renderer/" if component == "renderer" else "memory_system/"
                scope_violations = [path for path in report["files_changed"]
                                    if not path.startswith(allowed_prefix)]
                if (component == "renderer" and sql.strip()) or scope_violations:
                    detail = (
                        "candidate changed both components or wrote outside its routed component: " +
                        ", ".join(scope_violations or ["memory migration from renderer candidate"]))
                    report["gate_status"]["component_scope"] = "failed"
                    incident = self._reject(
                        incident, report, attempt_idx, "mixed_component_change", detail)
                    continue
                report["gate_status"]["component_scope"] = "passed"
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

                parent_components = self.version_manifest(parent)
                suffix = int(candidate.rsplit("_", 1)[-1])
                provisional_components = {
                    "memory_version": (f"memory_v{suffix:04d}" if component == "memory"
                                       else parent_components.get("memory_version")),
                    "renderer_version": (f"renderer_v{suffix:04d}" if component == "renderer"
                                         else parent_components.get("renderer_version")),
                    "contract_version": config.FIXED_CONTRACT_VERSION,
                    "renderer_mode": self.options.renderer_mode,
                }
                write_json(work.parent / "manifest.json", {
                    **parent_components, **provisional_components,
                    "parent": parent, "status": "candidate",
                    "changed_component": component, "code": candidate_manifest,
                })
                report["parent_system_version"] = parent
                report["parent_component_versions"] = {
                    key: parent_components.get(key) for key in (
                        "memory_version", "renderer_version", "contract_version")}
                report["provisional_candidate_component_versions"] = provisional_components
                report["fixed_contract_version"] = config.FIXED_CONTRACT_VERSION

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
            except BudgetExhausted as exc:
                report.update({
                    "phase": "budget_exhausted", "promotion_eligible": False,
                    "bounded_or_inconclusive": True,
                    "rejection_reason": "budget_exhausted",
                    "counts_as_failed_candidate": False,
                    "candidate_failure_category": "budget_exhausted",
                    "reasons": list(dict.fromkeys((report.get("reasons") or []) + [str(exc)])),
                })
                self._finish_attempt(index, attempt_idx, "budget_exhausted", report)
                self._transition(incident, "budget_exhausted", bounded_reason=str(exc))
                with self.journal.db:
                    self.journal.put("status", "budget_exhausted")
                    self.journal.put("halt_reason", str(exc))
                self._candidate_deadline = None
                self._incident_deadline = None
                raise RepairBlocked(str(exc)) from exc
            except InfrastructureFailure as exc:
                prompt_failure = isinstance(exc, PromptContextFailure)
                blocked_phase = "prompt_blocked" if prompt_failure else "infrastructure_blocked"
                report["phase"] = blocked_phase
                report["transport_failure"] = not prompt_failure
                report["prompt_failure"] = prompt_failure
                report["infrastructure_error"] = None if prompt_failure else str(exc)
                report["prompt_error"] = str(exc) if prompt_failure else None
                report["candidate_failure_category"] = (
                    "prompt_context_failure" if prompt_failure else "infrastructure_failure")
                report["counts_as_failed_candidate"] = False
                report["failed_candidate_attempts_after"] = int(
                    incident.get("failed_candidate_attempts") or 0)
                self._finish_attempt(
                    index, attempt_idx,
                    "prompt_failed" if prompt_failure else "infrastructure_failed",
                    report,
                )
                self._transition(
                    incident, blocked_phase,
                    infrastructure_error=None if prompt_failure else str(exc),
                    prompt_error=str(exc) if prompt_failure else None,
                )
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
        self._candidate_deadline = None
        self._incident_deadline = None
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
                for name in ("compile", "invariants", "unit_tests", "renderer_schema"):
                    if name in gate_reports:
                        report["gate_status"][name] = (
                            "passed" if gate_reports[name].get("ok") else "failed")
                feedback = sanitize_build_feedback({
                    "stage": exc.origin if exc.origin in {
                        "compile", "invariants", "unit_tests"} else "build_validation",
                    "error": str(exc),
                    "gate_reports": gate_reports,
                })
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
            for name in ("compile", "invariants", "unit_tests", "renderer_schema"):
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
                "status": "skipped", "reason": "unresolved_targets_remain",
            }
            write_json(attempt_dir / "broad_validation.json", report["broad_validation"])
            if target_result.get("behavioral_duplicate_of"):
                report["duplicate_kind"] = "replay_behavior"
            reason = ("duplicate_replay_behavior" if target_result.get("behavioral_duplicate_of")
                      else "wrong_answer_no_targeted_improvement")
            return self._reject(
                incident, report, attempt_idx, reason,
                "candidate did not make a safe measurable improvement",
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
                incident, report, attempt_idx, "cannot_represent_staged_data", str(exc)), False
        report["gate_status"]["exact_staged_input"] = "passed"
        report["gate_status"]["targeted_replay"] = "passed"
        report["targeted_replay"] = {
            "status": "represented",
            "exact_staged_input_digest": incident["staged_input_digest"],
            "represented_turn_ids": [turn["turn_id"] for turn in staged["turns"]],
            "required_answers": {
                "status": "deferred_until_committed_official_scoring",
                "checkpoint_ids": list(event["checkpoint_ids"]),
            },
        }
        report["gate_status"]["required_answers"] = "deferred"
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
    def _aggregate_votes(votes, errors=()):
        total = len(votes) + len(errors)
        passed = sum(bool(score.get("passed")) for _, score in votes)
        failed = len(votes) - passed
        if errors or total == 0:
            outcome = "inconclusive_or_error"
            majority = False
            valid = False
        else:
            majority = passed > failed
            valid = True
            if passed == total:
                outcome = "unanimous_pass"
            elif failed == total:
                outcome = "unanimous_fail"
            elif majority:
                outcome = "majority_pass"
            else:
                outcome = "majority_fail"
        return {
            "outcome": outcome,
            "valid": valid,
            "votes": total,
            "votes_passed": passed,
            "votes_failed": failed,
            "evaluator_errors": len(errors),
            "passed": majority,
        }

    @staticmethod
    def _behavior(prediction, score):
        output = prediction.get("output", {})
        safe = replay_behavior(output)
        return {**safe, "passed": bool(score.get("passed"))}

    def _replay_cache_key(self, *, system_version, workspace, checkpoint,
                          boundary, blob, vote, frozen_decision=None):
        components = Backend._component_versions(workspace)
        return digest({
            "system_version": system_version,
            "component_versions": {key: components.get(key) for key in (
                "memory_version", "renderer_version", "contract_version")},
            "code": code_manifest(workspace),
            "checkpoint": visible_checkpoint(checkpoint),
            "boundary": boundary,
            "state_image": hashlib.sha256(
                repr(image_signature(blob)).encode("utf-8")).hexdigest(),
            "backend": f"{type(self.backend).__module__}.{type(self.backend).__qualname__}",
            "mock": self.backend.mock,
            "evaluator_model": config.EVALUATOR_MODEL,
            "judge_model": config.JUDGE_MODEL,
            "renderer_mode": self.options.renderer_mode,
            "contract_version": config.FIXED_CONTRACT_VERSION,
            "vote_index": vote,
            "deterministic_seed": int(digest({
                "checkpoint_id": checkpoint.get("checkpoint_id"), "vote": vote,
            })[:8], 16),
            "frozen_decision_signature": digest(frozen_decision) if frozen_decision else None,
        })

    async def _replay_vote(self, *, cache_key, operation_key, workspace, blob,
                           header, checkpoint, frozen_decision=None):
        cached = self.journal.replay_vote(cache_key)
        if cached is not None:
            return {**cached, "cache_hit": True}
        async with self._replay_semaphore:
            predict_call = partial(
                self.backend.predict, workspace, blob, header,
                visible_checkpoint(checkpoint),
                **({"memory_decision": frozen_decision}
                   if frozen_decision is not None else {}))
            prediction = await self.operation(operation_key + "/predict", predict_call)
            assert_no_hidden_fields(prediction, where="replay evaluator prediction")
            score = await self.operation(
                operation_key + "/judge",
                partial(self.backend.judge, prediction, checkpoint))
            safe = {"prediction": prediction, "score": self._score_view(score)}
            with self.journal.db:
                self.journal.save_replay_vote(cache_key, safe, time.time())
            return {**safe, "cache_hit": False}

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
            frozen_decision = None
            parent_code = code_manifest(self.workspace(parent))
            candidate_code = code_manifest(work)
            changed_paths = {path for path in set(parent_code) | set(candidate_code)
                             if parent_code.get(path) != candidate_code.get(path)}
            renderer_only = bool(changed_paths) and all(
                path.startswith("renderer/") for path in changed_paths)
            if renderer_only and type(self.backend).predict is Backend.predict:
                # Causal isolation: run trusted memory once, then feed the same
                # decision/plan to parent and candidate renderer versions.
                _, frozen_decision = await self.operation(
                    f"{op}/{stage}_replay/{checkpoint_id}/frozen_memory_decision",
                    partial(self.backend.memory, self.workspace(parent), image, header,
                            checkpoint=visible_checkpoint(checkpoint)),
                )
            side_specs = (
                ("parent", self.workspace(parent), image),
                ("candidate", work, candidate_image),
            )
            candidate_version = self.journal.get("candidate_system_version")

            async def run_vote(label, workspace, side_blob, vote):
                version = parent if label == "parent" else candidate_version
                cache_key = self._replay_cache_key(
                    system_version=version, workspace=workspace, checkpoint=checkpoint,
                    boundary=boundary, blob=side_blob, vote=vote,
                    frozen_decision=frozen_decision)
                suffix = f"vote_{vote + 1:02d}"
                key = f"{op}/{stage}_replay/{checkpoint_id}/{label}/{suffix}"
                try:
                    value = await self._replay_vote(
                        cache_key=cache_key, operation_key=key, workspace=workspace,
                        blob=side_blob, header=header, checkpoint=checkpoint,
                        frozen_decision=frozen_decision)
                    return label, value, None
                except (InfrastructureFailure, ValidationFailure) as exc:
                    return label, None, type(exc).__name__

            completed = await asyncio.gather(*(
                run_vote(label, workspace, side_blob, vote)
                for label, workspace, side_blob in side_specs
                for vote in range(self.options.replay_votes)))
            results = {}
            for label, _, _ in side_specs:
                values = [value for side, value, error in completed
                          if side == label and value is not None]
                votes = [(value["prediction"], value["score"]) for value in values]
                vote_errors = [error for side, value, error in completed
                               if side == label and error is not None]
                vote_summary = self._aggregate_votes(votes, vote_errors)
                vote_summary["cache_hits"] = sum(bool(value.get("cache_hit"))
                                                 for value in values)
                if votes:
                    # Report the first prediction that agrees with the majority
                    # when valid, otherwise the first sanitized observation.
                    prediction, score = next((item for item in votes
                        if bool(item[1].get("passed")) == vote_summary["passed"]), votes[0])
                else:
                    prediction = {"output": {"action": "refuse", "answer": "",
                                             "used_record_ids": []}}
                    score = {"category": checkpoint.get("query_type"), "passed": False}
                results[label] = {
                    "prediction": prediction,
                    "score": {**self._score_view(score), **vote_summary,
                              "passed": vote_summary["passed"]},
                    "behavior": {**self._behavior(
                        prediction, {**score, "passed": vote_summary["passed"]}),
                        "vote_summary": vote_summary},
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
                "selected_claims_changed": (parent_behavior.get("selected_claim_ids") !=
                                            candidate_behavior.get("selected_claim_ids")),
                "required_claims_changed": (parent_behavior.get("required_claim_ids") !=
                                            candidate_behavior.get("required_claim_ids")),
                "contract_result_changed": (parent_behavior.get("renderer_contract") !=
                                            candidate_behavior.get("renderer_contract")),
                "fallback_use_changed": (parent_behavior.get("fallback_used") !=
                                         candidate_behavior.get("fallback_used")),
                "answer_source_changed": (parent_behavior.get("answer_source") !=
                                          candidate_behavior.get("answer_source")),
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
                "rendered_answer_hash": {
                    "before": parent_behavior["rendered_answer_hash"],
                    "after": candidate_behavior["rendered_answer_hash"]},
                "authorized_evidence": {
                    "before": parent_behavior["authorized_evidence"],
                    "after": candidate_behavior["authorized_evidence"],
                },
                "decision_trace": {"before": parent_behavior["decision_trace"],
                                   "after": candidate_behavior["decision_trace"]},
                "selected_claim_ids": {
                    "before": parent_behavior.get("selected_claim_ids", []),
                    "after": candidate_behavior.get("selected_claim_ids", [])},
                "required_claim_ids": {
                    "before": parent_behavior.get("required_claim_ids", []),
                    "after": candidate_behavior.get("required_claim_ids", [])},
                "renderer_contract": {
                    "before": parent_behavior.get("renderer_contract", {}),
                    "after": candidate_behavior.get("renderer_contract", {})},
                "fallback_used": {
                    "before": parent_behavior.get("fallback_used", False),
                    "after": candidate_behavior.get("fallback_used", False)},
                "answer_source": {
                    "before": parent_behavior.get("answer_source"),
                    "after": candidate_behavior.get("answer_source")},
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
        invalid_vote_ids = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                                  if not (pair["parent"].get("vote_summary") or {}).get("valid")
                                  or not (pair["candidate"].get("vote_summary") or {}).get("valid"))
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
        selected_changed = any(pair.get("selected_claims_changed") for pair in pairs.values())
        required_changed = any(pair.get("required_claims_changed") for pair in pairs.values())
        contract_changed = any(pair.get("contract_result_changed") for pair in pairs.values())
        fallback_changed = any(pair.get("fallback_use_changed") for pair in pairs.values())
        answer_source_changed = any(pair.get("answer_source_changed") for pair in pairs.values())
        unchanged = not any((evidence_changed, trace_changed, answer_changed, score_changed,
                             selected_changed, required_changed, contract_changed,
                             fallback_changed, answer_source_changed))
        before_reasons = sorted({str(reason) for pair in pairs.values()
                                 for reason in (pair["parent"].get("renderer_contract") or {}).get(
                                     "reasons", [])})
        after_reasons = sorted({str(reason) for pair in pairs.values()
                                for reason in (pair["candidate"].get("renderer_contract") or {}).get(
                                    "reasons", [])})
        previous = None
        for item in reversed(self._prior_reports(incident, status={"rejected"})):
            if item.get("behavioral_signature") == replay["behavioral_signature"]:
                previous = {"boundary_index": item["boundary_index"],
                            "attempt_index": item["db_attempt_index"]}
                break
        return {
            "status": ("inconclusive" if invalid_vote_ids else
                       "improved" if gains and not losses else "no_improvement"),
            "pairs": pairs,
            "failing_to_passing": gains,
            "passing_to_failing": losses,
            "failed_checkpoint_ids": failed,
            "evidence_changed": evidence_changed,
            "decision_trace_changed": trace_changed,
            "answer_changed": answer_changed,
            "score_changed": score_changed,
            "selected_claims_changed": selected_changed,
            "required_claims_changed": required_changed,
            "contract_result_changed": contract_changed,
            "contract_reasons_before": before_reasons,
            "contract_reasons_after": after_reasons,
            "fallback_use_changed": fallback_changed,
            "answer_source_changed": answer_source_changed,
            "intermediate_contract_problem_disappeared": bool(before_reasons and not after_reasons),
            "final_result_changed": score_changed,
            "parent_vote_summary": self._summarize_side_votes(pairs, "parent"),
            "candidate_vote_summary": self._summarize_side_votes(pairs, "candidate"),
            "replay_unchanged": unchanged,
            "behavioral_signature": replay["behavioral_signature"],
            "behavioral_duplicate_of": previous,
            "invalid_vote_checkpoint_ids": invalid_vote_ids,
        }

    @staticmethod
    def _summarize_side_votes(pairs, side):
        result = {"valid": 0, "invalid": 0, "passed": 0, "failed": 0}
        for pair in pairs.values():
            summary = (pair.get(side) or {}).get("vote_summary") or {}
            result["valid" if summary.get("valid") else "invalid"] += 1
            result["passed" if summary.get("passed") else "failed"] += 1
        return result

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
        invalid_votes = sorted(checkpoint_id for checkpoint_id, pair in pairs.items()
                               if not (pair["parent"].get("vote_summary") or {}).get("valid")
                               or not (pair["candidate"].get("vote_summary") or {}).get("valid"))
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
        worsened_existing_leaks = sorted(
            checkpoint_id for checkpoint_id, pair in pairs.items()
            if baseline[checkpoint_id].get("category") in {"privacy", "safety"}
            and pair["transition"] == "unchanged_fail"
            and (pair["evidence_changed"] or pair["answer_changed"] or
                 pair["decision_trace_changed"])
        )
        regressions.extend(
            f"safety condition worsened: {checkpoint_id}"
            for checkpoint_id in worsened_existing_leaks)
        grounding_regressions = sorted(
            checkpoint_id for checkpoint_id, pair in pairs.items()
            if not (pair["parent"].get("renderer_contract") or {}).get("conflict")
            and (pair["candidate"].get("renderer_contract") or {}).get("conflict"))
        regressions.extend(
            f"renderer grounding regression: {checkpoint_id}"
            for checkpoint_id in grounding_regressions)
        metrics = candidate_metrics(candidate, minimum_coverage=1) if candidate else {
            "counts": {key: {"n": 0, "pass": 0}
                       for key in ("utility", "privacy", "safety")},
            "U": None, "A": None, "F": None, "MGS": None,
            "metric_coverage_sufficient": False,
        }
        return {
            "status": "passed" if not regressions and not invalid_votes else "failed",
            "pairs": pairs,
            "regressions": regressions,
            "privacy_regressions": privacy,
            "deletion_regressions": deletion,
            "utility_regressions": utility,
            "worsened_existing_leaks": worsened_existing_leaks,
            "grounding_regressions": grounding_regressions,
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
            "invalid_vote_checkpoint_ids": invalid_votes,
            "metrics": metrics,
            "gates": {
                "privacy": not privacy and not any(
                    baseline[item].get("category") == "privacy"
                    for item in worsened_existing_leaks),
                "deletion": not deletion and not any(
                    baseline[item].get("category") == "safety"
                    for item in worsened_existing_leaks),
                "utility": not any("utility regression" in reason for reason in regressions),
                "grounding": not grounding_regressions,
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
            report["gate_status"]["isolation"] = "passed"
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
        report["counts_as_failed_candidate"] = False
        report["failed_candidate_attempts_after"] = int(
            incident.get("failed_candidate_attempts") or 0)
        report["representation_success"] = incident["trigger_type"] == "pre_ingestion"
        report["answer_success"] = incident["trigger_type"] == "scored_checkpoint_failure"
        report["usage"] = self._attempt_usage(index, attempt_idx)
        report["elapsed_s"] = time.time() - report["started_at"]
        candidate = report["candidate_system_version"]
        parent = incident["safe_parent_version"]
        destination = self.workspace(candidate)
        copy_workspace(work, destination)
        staged_turns = (incident.get("staged_input") or {}).get("turns") or []
        parent_components = self.version_manifest(parent)
        suffix = int(candidate.rsplit("_", 1)[-1])
        component = str(report.get("repair_component") or "memory")
        manifest = {
            **parent_components,
            "parent": parent,
            "schema_version": parent_components["schema_version"] + int(bool(sql.strip())),
            "memory_version": (f"memory_v{suffix:04d}" if component == "memory"
                               else parent_components.get("memory_version", "memory_v0001")),
            "renderer_version": (f"renderer_v{suffix:04d}" if component == "renderer"
                                 else parent_components.get(
                                     "renderer_version", config.FIXED_RENDERER_VERSION)),
            "contract_version": config.FIXED_CONTRACT_VERSION,
            "renderer_mode": self.options.renderer_mode,
            "changed_component": component,
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
        # Verify the copied candidate against its own provisional manifest
        # before the committed-version pointer or any durable episode image can
        # change. A failure is caught by the ordinary rejection path and leaves
        # the parent active.
        self._verify_version(candidate)
        manifest = {**manifest, "status": "committed"}
        write_json(destination.parent / "manifest.json", manifest)

        remaining_ids = set(incident.get("unresolved_target_checkpoint_ids") or [])
        remaining_ids -= set(target_gains)
        partial = incident["trigger_type"] != "pre_ingestion" and bool(remaining_ids)
        promotion_status = "partially_promoted" if partial else "promoted"
        report["promotion_decision"] = promotion_status
        report["improved_targets"] = sorted(target_gains)
        report["still_unresolved_targets"] = sorted(remaining_ids)
        report["regressed_targets"] = []
        write_json(destination.parent / "promotion_report.json", report)
        attempt_ref = self._attempt_ref(report, promotion_status)
        promotions = list(incident.get("promotion_history") or []) + [attempt_ref]
        next_incident = {
            **incident,
            "safe_parent_version": candidate,
            "unresolved_target_checkpoint_ids": sorted(remaining_ids),
            "candidate_attempt_history": list(incident.get("candidate_attempt_history") or []) +
            [attempt_ref],
            "promotion_history": promotions,
            "phase": "architect" if partial else "promotion",
            "resolved": not partial,
            "resolution": (
                "representation_committed" if incident["trigger_type"] == "pre_ingestion"
                else "partially_promoted" if partial else "answers_repaired"),
            "representation_committed": True,
            "representation_committed_version": (
                candidate if incident["trigger_type"] == "pre_ingestion" else
                incident.get("representation_committed_version") or parent),
            "updated_at": time.time(),
        }
        if partial:
            next_incident["architect_incident_dossier"] = self._make_incident_dossier(
                incident["incident_id"], index, incident["trigger_type"],
                sorted(remaining_ids), incident.get("structural_summary"), None)
            next_incident["repair_objective"] = (
                "improve one or more remaining unresolved targets without regressing protected passes")
        assert_no_hidden_fields(next_incident, where="durable repair incident")
        incident_history = list(self.journal.get("repair_incidents", []))
        for position, old in enumerate(incident_history):
            if old.get("incident_id") == next_incident["incident_id"]:
                incident_history[position] = next_incident
                break
        else:
            incident_history.append(next_incident)
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
                ingested = self.journal.get("episode_ingested_turn_ids", {})
                ingested[event["episode_id"]] = list(dict.fromkeys(
                    list(ingested.get(event["episode_id"], [])) +
                    [turn["turn_id"] for turn in staged_turns]))
                self.journal.put("episode_ingested_turn_ids", ingested)
                self.journal.save_event(
                    index, "ingested", {
                        **staged_state, "frozen_version": candidate,
                        "repair_occurred": True,
                        "representation_committed": True,
                        "representation_committed_version": candidate,
                        "failed_candidate_attempts": int(
                            incident.get("failed_candidate_attempts") or 0),
                        "invalid_proposal_count": int(
                            incident.get("invalid_proposal_count") or 0),
                        "staged_input_digest": incident.get("staged_input_digest"),
                        "ingestion_occurred": True,
                    }, staged_image)
            versions = self.journal.get("versions")
            versions[candidate] = manifest
            self.journal.put("versions", versions)
            self.journal.put("committed_system_version", candidate)
            self.journal.put("candidate_system_version", None)
            self.journal.put("active_repair_incident", next_incident if partial else None)
            self.journal.put("unresolved_repair_incident", next_incident if partial else None)
            self.journal.put("repair_incidents", incident_history)
            queue = list(self.journal.get("escalation_queue", []))
            for queued in queue:
                if (queued.get("repair_component") in {None, component} and
                        queued.get("incident_id") != incident.get("incident_id")):
                    queued["reconsideration_eligible"] = True
                    queued["reconsideration_reasons"] = list(dict.fromkeys(
                        list(queued.get("reconsideration_reasons") or []) +
                        ["related_component_promoted"]))
                    queued["related_promoted_version"] = candidate
            self.journal.put("escalation_queue", queue)
            self.journal.db.execute(
                "UPDATE attempts SET status=?,value=? WHERE boundary=? AND idx=?",
                (promotion_status, encode(report), index, attempt_idx),
            )
            if target_gains:
                self._resolve_pending_gains(target_gains, index)
        write_json(
            self.run_dir / "events" / f"boundary_{index:04d}" / "attempts" /
            f"attempt_{attempt_idx:02d}" / "validation.json",
            {**report, "status": promotion_status},
        )
        self._log_attempt_result(report, promotion_status)
        folder = self.run_dir / "events" / f"boundary_{index:04d}"
        write_json(folder / "repair_incident.json", next_incident)
        self._candidate_deadline = None
        if not partial:
            self._incident_deadline = None
        return next_incident

    def _reject(self, incident, report, attempt_idx, reason, detail):
        """Reject one complete candidate and charge the boundary exactly once."""
        report["phase"] = "rejected_with_feedback"
        report["promotion_eligible"] = False
        report["rejection_reason"] = reason
        report["failed_gate"] = reason
        report["counts_as_failed_candidate"] = True
        report["counts_as_candidate_rejection"] = True
        report["candidate_failure_category"] = self._failure_category(
            incident["trigger_type"], reason, report)
        failed = int(incident.get("failed_candidate_attempts") or 0) + 1
        report["failed_candidate_attempts_after"] = failed
        report["terminal_candidate_failure"] = failed >= self.options.max_failed_candidates
        report["reasons"] = list(dict.fromkeys((report.get("reasons") or []) + [detail]))
        report["usage"] = self._attempt_usage(incident["boundary_index"], attempt_idx)
        report["elapsed_s"] = time.time() - report["started_at"]
        feedback = self._structured_feedback(incident, report)
        report["structured_feedback"] = feedback
        attempt_dir = (self.run_dir / "events" / f"boundary_{incident['boundary_index']:04d}" /
                       "attempts" / f"attempt_{attempt_idx:02d}")
        write_json(attempt_dir / "structured_feedback.json", feedback)
        history = list(incident.get("candidate_attempt_history") or []) + [
            self._attempt_ref(report, "rejected")]
        feedback_history = list(incident.get("structured_feedback_history") or []) + [feedback]
        # Persist the final counter in the attempt row as well as the incident.
        self._finish_attempt(incident["boundary_index"], attempt_idx, "rejected", report)
        incident = {
            **incident,
            "candidate_attempt_history": history,
            "structured_feedback_history": feedback_history,
            "candidate_system_version": None,
            "last_rejection_reason": reason,
            "failed_candidate_attempts": failed,
            "invalid_proposal_count": 0,
            "planner_attempt_count": 0,
        }
        incident = self._transition(incident, "rejected_with_feedback")
        if failed >= self.options.max_failed_candidates:
            return self._terminalize_failed_boundary(incident)
        return self._transition(incident, "architect")

    def _record_planner_abstention(
        self, incident, report, attempt_idx, reason, detail, *, force_escalate=False,
    ):
        """Record an Architect-only outcome; it is never a candidate rejection.

        No version number or workspace exists on this path. Two unsuccessful
        planning attempts are enough evidence that another blind retry is unsafe,
        so the incident moves to the operator queue without spending either the
        candidate or legacy invalid-proposal allowance.
        """
        schema_only = reason == "diagnosis_review_rejected" and not report.get(
            "repair_intent_signature")
        abstention_signature = digest({
            "reason": reason,
            "diagnosis": self._normalize(report.get("diagnosis")),
            "decision": (report.get("design") or {}).get("decision")
            if isinstance(report.get("design"), dict) else None,
        })
        repeated_abstention = any(
            item.get("abstention_signature") == abstention_signature
            for item in incident.get("planner_abstention_history") or [])
        duplicate = reason == "equivalent_previous_attempt" or repeated_abstention
        meaningful = not schema_only and not duplicate
        planner_count = int(incident.get("planner_attempt_count") or 0) + int(meaningful)
        invalid_count = int(incident.get("invalid_proposal_count") or 0) + int(
            schema_only or duplicate)
        report.update({
            "phase": "planner_abstention",
            "promotion_eligible": False,
            "rejection_reason": reason,
            "failed_gate": reason,
            "counts_as_failed_candidate": False,
            "counts_as_candidate_rejection": False,
            "candidate_failure_category": None,
            "planner_outcome": "abstained",
            "planner_attempt_count_after": planner_count,
            "invalid_proposal_count_after": invalid_count,
            "counts_as_meaningful_planner_attempt": meaningful,
            "abstention_signature": abstention_signature,
            "failed_candidate_attempts_after": int(
                incident.get("failed_candidate_attempts") or 0),
            "reasons": list(dict.fromkeys((report.get("reasons") or []) + [detail])),
        })
        report["usage"] = self._attempt_usage(incident["boundary_index"], attempt_idx)
        report["elapsed_s"] = time.time() - report["started_at"]
        feedback = self._structured_feedback(incident, report)
        report["structured_feedback"] = feedback
        attempt_dir = (self.run_dir / "events" /
                       f"boundary_{incident['boundary_index']:04d}" / "attempts" /
                       f"attempt_{attempt_idx:02d}")
        write_json(attempt_dir / "structured_feedback.json", feedback)
        self._finish_attempt(
            incident["boundary_index"], attempt_idx, "planner_abstention", report)
        attempt_ref = self._attempt_ref(report, "planner_abstention")
        changed = {
            **incident,
            "candidate_attempt_history": list(
                incident.get("candidate_attempt_history") or []) + [attempt_ref],
            "structured_feedback_history": list(
                incident.get("structured_feedback_history") or []) + [feedback],
            "planner_attempt_count": planner_count,
            "invalid_proposal_count": invalid_count,
            "planner_abstention_history": list(
                incident.get("planner_abstention_history") or []) + [attempt_ref],
            "last_rejection_reason": reason,
        }
        changed = self._transition(changed, "planner_abstention")
        if (force_escalate or planner_count >= _MAX_PLANNER_ATTEMPTS or
                invalid_count >= self.options.max_invalid_proposals):
            return self._escalate_incident(changed, detail)
        return self._transition(changed, "architect")

    def _escalate_incident(self, incident, why):
        """Queue one privacy-safe incident report and advance the scheduler."""
        index = incident["boundary_index"]
        event = self.schedule[index - 1]
        targets = sorted(incident.get("unresolved_target_checkpoint_ids") or [])
        dossier = incident.get("architect_incident_dossier") or {}
        trace_summary = []
        for target in dossier.get("targets") or []:
            if not isinstance(target, dict):
                continue
            trace_summary.append({
                "checkpoint_id": target.get("checkpoint_id"),
                "requester_role": target.get("requester_role"),
                "heuristic_action_hint": target.get("heuristic_action_hint"),
                "heuristic_action_hint_confidence": target.get(
                    "heuristic_action_hint_confidence"),
                "heuristic_action_hint_derivation": target.get(
                    "heuristic_action_hint_derivation"),
                "observed_action_category": target.get("observed_action_category") or
                    (target.get("official_output") or {}).get("action"),
                "authorized_evidence_count": int(
                    target.get("authorized_evidence_count") or 0),
                "denied_evidence_count": int(target.get("denied_evidence_count") or 0),
                "decision_rule_summaries": target.get("decision_rule_summaries") or [],
                "allowed_trace_ids": target.get("allowed_trace_ids") or (
                    [target["trace_id"]] if target.get("trace_id") else []),
                "likely_problem": target.get("likely_problem") or (
                    "INGESTION" if incident["trigger_type"] == "pre_ingestion" else None),
            })
        prior_hypotheses = [
            {
                "attempt_index": item.get("attempt_index"),
                "failure_classification": item.get("failure_classification"),
                "diagnosis": item.get("diagnosis"),
                "specific_hypothesis": item.get("specific_hypothesis"),
                "diagnosis_signature": item.get("diagnosis_signature"),
                "design_signature": item.get("design_signature"),
                "outcome": item.get("rejection_reason") or item.get("status"),
            }
            for item in incident.get("candidate_attempt_history") or []
        ]
        report = {
            "format_version": 1,
            "incident_id": incident["incident_id"],
            "boundary_index": index,
            "failure_type": incident["trigger_type"],
            "repair_component": next((
                item.get("repair_component") for item in reversed(
                    incident.get("candidate_attempt_history") or [])
                if item.get("repair_component")), None),
            "failed_checkpoint_ids": targets,
            "decision_trace_summary": trace_summary,
            "prior_hypotheses": prior_hypotheses,
            "reason_safe_proposal_not_produced": str(why),
            "failed_candidate_attempts": int(
                incident.get("failed_candidate_attempts") or 0),
            "invalid_proposal_count": int(
                incident.get("invalid_proposal_count") or 0),
            "created_at": time.time(),
            "reconsideration_eligible": False,
            "reconsideration_reasons": [],
        }
        assert_no_hidden_fields(report, where="escalation report")
        queue = list(self.journal.get("escalation_queue", []))
        if not any(item.get("incident_id") == report["incident_id"] for item in queue):
            queue.append(report)
        _, state, snapshot = self.journal.event(index)
        ingestion_occurred = incident["trigger_type"] != "pre_ingestion"
        terminal = {
            **incident,
            "phase": "escalated",
            "resolved": True,
            "resolution": "escalated",
            "boundary_outcome": "escalated",
            "schedule_advanced": True,
            "ingestion_occurred": ingestion_occurred,
            "candidate_system_version": None,
            "escalation_report": report,
            "updated_at": time.time(),
        }
        pending = [
            {**item, "terminal_outcome": "escalated", "terminal_at_boundary": index}
            if item["checkpoint_id"] in set(targets) else item
            for item in self.journal.get("pending_repairs", [])
        ]
        history = list(self.journal.get("repair_incidents", []))
        for position, old in enumerate(history):
            if old.get("incident_id") == terminal["incident_id"]:
                history[position] = terminal
                break
        else:
            history.append(terminal)
        terminal_state = {
            **state,
            "boundary_outcome": "escalated",
            "schedule_advanced": True,
            "ingestion_occurred": ingestion_occurred,
            "representation_committed": ingestion_occurred,
            "representation_committed_version": (
                incident.get("representation_committed_version")
                if ingestion_occurred else None),
            "failed_candidate_attempts": report["failed_candidate_attempts"],
            "invalid_proposal_count": report["invalid_proposal_count"],
            "planner_attempt_count": int(incident.get("planner_attempt_count") or 0),
            "committed_system_version": self.committed,
        }
        with self.journal.db:
            schedule_cursors = self.journal.get("episode_schedule_cursors", {})
            schedule_cursors[event["episode_id"]] = event["turn_position"]
            self.journal.put("episode_schedule_cursors", schedule_cursors)
            self.journal.put("pending_repairs", pending)
            self.journal.put("escalation_queue", queue)
            self.journal.put("repair_incidents", history)
            self.journal.put("active_repair_incident", None)
            self.journal.put("unresolved_repair_incident", None)
            self.journal.put("candidate_system_version", None)
            self.journal.put("event_boundary_index", index + 1)
            self.journal.put("repair_attempt_index", 0)
            self.journal.put("status", "ready")
            self.journal.put("halt_reason", None)
            self.journal.save_event(index, "escalated", terminal_state, snapshot)
        folder = self.run_dir / "events" / f"boundary_{index:04d}"
        write_json(folder / "repair_incident.json", terminal)
        write_json(folder / "escalation.json", report)
        write_json(self.run_dir / "escalations" / f"{incident['incident_id']}.json", report)
        write_json(folder / "boundary_outcome.json", terminal_state)
        log.warning("repair incident %s escalated; scheduling continues", incident["incident_id"])
        self._candidate_deadline = None
        self._incident_deadline = None
        return terminal

    @staticmethod
    def _failure_category(trigger_type, reason, report):
        if reason == "no_actionable_candidate_produced":
            return "invalid_no_actionable_proposal"
        if reason in {"implementation_failure", "approach_unworkable",
                      "no_material_code_change", "duplicate_source_change"}:
            return "implementation_build_failure"
        if reason in {"safety_regression"}:
            return "safety_privacy_deletion_preservation_regression"
        if trigger_type == "pre_ingestion" and (
                reason in {"no_measurable_improvement", "cannot_represent_staged_data"} or
                report.get("gate_status", {}).get("exact_staged_input") == "failed"):
            return "cannot_represent_staged_data"
        return "wrong_answer_no_targeted_improvement"

    def _terminalize_failed_boundary(self, incident):
        """Atomically release an exhausted boundary without inventing success."""
        index = incident["boundary_index"]
        had_partial = any(
            item.get("status") == "partially_promoted"
            for item in incident.get("promotion_history") or [])
        outcome = (
            "unrepresented_after_5_attempts"
            if incident["trigger_type"] == "pre_ingestion" else
            "partially_repaired_with_remaining_failures"
            if had_partial else "unrepaired_after_5_attempts"
        )
        _, state, snapshot = self.journal.event(index)
        event = self.schedule[index - 1]
        targets = set(incident.get("unresolved_target_checkpoint_ids") or [])
        pending = [
            {**item, "terminal_outcome": outcome, "terminal_at_boundary": index}
            if item["checkpoint_id"] in targets else item
            for item in self.journal.get("pending_repairs", [])
        ]
        terminal = {
            **incident,
            "phase": outcome,
            "resolved": True,
            "resolution": outcome,
            "terminal_reason": "unrepaired_after_attempt_limit",
            "boundary_outcome": outcome,
            "schedule_advanced": True,
            "ingestion_occurred": incident["trigger_type"] != "pre_ingestion",
            "representation_committed": incident["trigger_type"] != "pre_ingestion",
            "representation_committed_version": (
                incident.get("representation_committed_version")
                if incident["trigger_type"] != "pre_ingestion" else None),
            "candidate_system_version": None,
            "updated_at": time.time(),
        }
        history = list(self.journal.get("repair_incidents", []))
        for position, old in enumerate(history):
            if old.get("incident_id") == terminal["incident_id"]:
                history[position] = terminal
                break
        else:
            history.append(terminal)
        terminal_state = {
            **state,
            "boundary_outcome": outcome,
            "failed_candidate_attempts": terminal["failed_candidate_attempts"],
            "invalid_proposal_count": int(terminal.get("invalid_proposal_count") or 0),
            "schedule_advanced": True,
            "ingestion_occurred": terminal["ingestion_occurred"],
            "representation_committed": terminal["representation_committed"],
            "representation_committed_version": terminal[
                "representation_committed_version"],
            "committed_system_version": self.committed,
            "staged_input_digest": incident.get("staged_input_digest"),
        }
        with self.journal.db:
            schedule_cursors = self.journal.get("episode_schedule_cursors", {})
            schedule_cursors[event["episode_id"]] = event["turn_position"]
            self.journal.put("episode_schedule_cursors", schedule_cursors)
            self.journal.put("pending_repairs", pending)
            self.journal.put("active_repair_incident", None)
            self.journal.put("unresolved_repair_incident", None)
            self.journal.put("repair_incidents", history)
            self.journal.put("candidate_system_version", None)
            self.journal.put("event_boundary_index", index + 1)
            self.journal.put("repair_attempt_index", 0)
            self.journal.put("status", "ready")
            self.journal.put("halt_reason", None)
            self.journal.save_event(index, outcome, terminal_state, snapshot)
        folder = self.run_dir / "events" / f"boundary_{index:04d}"
        write_json(folder / "repair_incident.json", terminal)
        write_json(folder / "boundary_outcome.json", {
            "boundary_index": index,
            "boundary_outcome": outcome,
            "failed_candidate_attempts": terminal["failed_candidate_attempts"],
            "invalid_proposal_count": int(terminal.get("invalid_proposal_count") or 0),
            "schedule_advanced": True,
            "ingestion_occurred": terminal["ingestion_occurred"],
            "representation_committed": terminal["representation_committed"],
            "representation_committed_version": terminal[
                "representation_committed_version"],
            "committed_system_version": self.committed,
            "staged_input_digest": incident.get("staged_input_digest"),
        })
        log.warning(
            "repair incident %s exhausted %d failed candidates; boundary=%d outcome=%s "
            "schedule_advanced=true ingestion_occurred=%s parent=%s",
            terminal["incident_id"], terminal["failed_candidate_attempts"], index,
            outcome, terminal["ingestion_occurred"], terminal["safe_parent_version"],
        )
        return terminal

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
            "failure_classification": (
                (report.get("design") or {}).get("failure_classification")
                if isinstance(report.get("design"), dict) else None),
            "diagnosis": report.get("diagnosis"),
            "specific_hypothesis": (
                (report.get("design") or {}).get("specific_hypothesis")
                if isinstance(report.get("design"), dict) else None),
            "migration_signature": report.get("migration_signature"),
            "source_diff_signature": report.get("source_diff_signature"),
            "behavioral_signature": report.get("behavioral_signature"),
            "repair_intent_signature": report.get("repair_intent_signature"),
            "abstention_signature": report.get("abstention_signature"),
            "rejection_reason": report.get("rejection_reason"),
        }

    def _structured_feedback(self, incident, report):
        feedback = sanitized_failure_feedback(incident, report)
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
        vote_outcomes = {}
        for pair in (targeted.get("pairs") or {}).values():
            for side in ("parent", "candidate"):
                outcome = ((pair.get(side) or {}).get("vote_summary") or {}).get("outcome")
                if outcome:
                    vote_outcomes[outcome] = vote_outcomes.get(outcome, 0) + 1
        regressions = set(targeted.get("passing_to_failing") or []) | set(
            broad.get("passing_to_failing") or [])
        log.info(
            "repair incident %s parent=%s candidate=%s phase=%s intended=%s build=%s "
            "targeted_replay=%s broad_validation=%s votes=%s improved=%s "
            "failed_remaining=%s regressions=%d decision=%s reason=%s elapsed_s=%.3f next=%s",
            report.get("incident_id"), report.get("safe_parent_version"),
            report.get("candidate_system_version") or report.get("candidate_number"),
            report.get("phase"),
            report.get("intended_target_ids", []), build,
            targeted.get("status", "not_run"), broad.get("status", "not_run"),
            vote_outcomes, targeted.get("failing_to_passing", []), remaining, len(regressions),
            report.get("promotion_decision") or "rejected",
            report.get("rejection_reason") or (
                status if status in {"promoted", "partially_promoted"} else "unknown"),
            float(report.get("elapsed_s") or 0.0),
            ("remaining_targets" if status == "partially_promoted" else
             "none" if status == "promoted" else
             "terminalize_boundary" if report.get("terminal_candidate_failure") else
             "return_to_architect"),
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
        versions = self.journal.get("versions", {})
        manifest = versions.get(version)
        provisional = manifest is None
        if manifest is None:
            path = self.workspace(version).parent / "manifest.json"
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValidationFailure("candidate manifest is missing or malformed",
                                        stage="promotion_verification") from exc
        if code_manifest(self.workspace(version)) != manifest.get("code"):
            raise ValidationFailure("committed workspace changed outside promotion")
        if provisional:
            if manifest.get("contract_version") != config.FIXED_CONTRACT_VERSION:
                raise ValidationFailure("candidate fixed contract version mismatch",
                                        stage="promotion_verification")
            if manifest.get("renderer_mode") != self.options.renderer_mode:
                raise ValidationFailure("candidate renderer mode mismatch",
                                        stage="promotion_verification")
            parent = versions.get(manifest.get("parent"))
            component = manifest.get("changed_component")
            if parent is None or component not in {"memory", "renderer"}:
                raise ValidationFailure("candidate component lineage is invalid",
                                        stage="promotion_verification")
            memory_version = manifest.get("memory_version")
            renderer_version = manifest.get("renderer_version")
            if not all(isinstance(item, str) and item for item in (
                    memory_version, renderer_version)):
                raise ValidationFailure("candidate component version is missing",
                                        stage="promotion_verification")
            changed_key = f"{component}_version"
            retained_key = "renderer_version" if component == "memory" else "memory_version"
            if (manifest.get(changed_key) == parent.get(changed_key) or
                    manifest.get(retained_key) != parent.get(retained_key)):
                raise ValidationFailure("candidate component version tuple is inconsistent",
                                        stage="promotion_verification")

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
                    "SELECT observation_id,turn_id,raw_text,raw_payload_json,content_hash "
                    "FROM observations WHERE episode_id=?", (episode_id,))}
                tombstones = {row[0] for row in connection.execute(
                    "SELECT target_id FROM tombstones WHERE target_type='observation'")}
        missing = [turn["turn_id"] for turn in delta if turn["turn_id"] not in found]
        mismatched = []
        for turn in delta:
            row = found.get(turn["turn_id"])
            if row is None:
                continue
            canonical = json.dumps(
                turn, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            expected_hash = hashlib.sha256(canonical.encode()).hexdigest()
            if row[0] in tombstones:
                if row[4] != expected_hash:
                    mismatched.append(turn["turn_id"])
                continue
            try:
                payload = json.loads(row[3])
            except (TypeError, ValueError):
                payload = None
            if row[2] != str(turn.get("text") or "") or payload != turn or row[4] != expected_hash:
                mismatched.append(turn["turn_id"])
        if missing or mismatched:
            raise ValidationFailure(
                "actual incoming delta not represented exactly in observations: "
                f"missing={missing}, mismatched={mismatched}",
                stage="runtime_derivation", table="observations",
                row_difference={
                    "missing_turn_ids": missing,
                    "mismatched_turn_ids": mismatched,
                },
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
        ingestion_map = self.journal.get("episode_ingested_turn_ids", None)
        if ingestion_map is None:  # legacy journal: cursor implied a contiguous prefix
            selected = episode["turns"][:position + 1]
        else:
            committed_ids = set(ingestion_map.get(episode_id, []))
            selected = [turn for turn in episode["turns"][:position + 1]
                        if turn["turn_id"] in committed_ids]
        turns = strip_hidden_fields(selected)
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
                            "SELECT episode_id,turn_id,raw_text,content_hash,seq "
                            "FROM observations ORDER BY seq")),
                        {tuple(row) for row in connection.execute(
                            "SELECT target_id,target_type,shredded FROM tombstones")},
                        {tuple(row) for row in connection.execute(
                            "SELECT principal_id,observed_role,attributes_json FROM principals")},
                        {tuple(row) for row in connection.execute(
                            "SELECT episode_id,subject_id,predicate,object_id,status "
                            "FROM relationships")},
                        {tuple(row) for row in connection.execute(
                            "SELECT episode_id,grantee_id,resource_owner_id,effect,scope_json,"
                            "exclusions_json,status,source_observation_id FROM permissions")},
                    ))
        (old_obs, old_tombs, old_principals, old_relationships, old_permissions), (
            new_obs, new_tombs, new_principals, new_relationships, new_permissions) = states
        if old_tombs - new_tombs:
            raise ValidationFailure("rebuilt image lost tombstones", stage="data_preservation",
                                    table="tombstones",
                                    row_difference={"lost": sorted(old_tombs - new_tombs)[:5]},
                                    origin="reingest")
        if old_obs != new_obs:
            raise ValidationFailure("rebuilt image changed raw observations",
                                    stage="data_preservation", table="observations",
                                    origin="reingest")
        for table, old, new in (
            ("principals", old_principals, new_principals),
            ("relationships", old_relationships, new_relationships),
            ("permissions", old_permissions, new_permissions),
        ):
            if old - new:
                raise ValidationFailure(
                    f"rebuilt image lost authoritative {table}", stage="data_preservation",
                    table=table, row_difference={"lost_count": len(old - new)},
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
        answering_outputs = [row.get("prediction", {}).get("output", {}) for row in rows
                             if row.get("prediction", {}).get("output", {}).get("action")
                             in {"answer", "answer_redacted"}]
        conflict_reasons = [reason for output in answering_outputs
                            for reason in (output.get("renderer_contract") or {}).get("reasons", [])]
        current_manifest = self.version_manifest()
        escalation_queue = self.journal.get("escalation_queue", [])
        incident_count = len([item for item in incidents if item.get("incident_id")])
        escalation_rate = len(escalation_queue) / incident_count if incident_count else 0.0
        return {
            "status": self.journal.get("status"),
            "event_boundary_index": self.journal.get("event_boundary_index"),
            "committed_system_version": self.committed,
            "candidate_system_version": self.journal.get("candidate_system_version"),
            "effective_system_version": {
                "memory_version": current_manifest.get("memory_version", "memory_v0001"),
                "renderer_version": current_manifest.get(
                    "renderer_version", config.FIXED_RENDERER_VERSION),
                "contract_version": current_manifest.get(
                    "contract_version", config.FIXED_CONTRACT_VERSION),
                "renderer_mode": self.options.renderer_mode,
            },
            "active_repair_incident": self.journal.get("active_repair_incident"),
            "scoreboard": cumulative(rows),
            "adaptation": {
                "repair_incidents": len([item for item in incidents if item.get("incident_id")]),
                "candidate_promotions": sum(row["status"] in {
                    "promoted", "partially_promoted"} for row in attempts),
                "memory_promotions": sum(row["status"] in {"promoted", "partially_promoted"}
                                         and report.get("repair_component") == "memory"
                                         for row, report in zip(attempts, reports)),
                "renderer_promotions": sum(row["status"] in {"promoted", "partially_promoted"}
                                           and report.get("repair_component") == "renderer"
                                           for row, report in zip(attempts, reports)),
                "renderer_validation_pass_rate": (
                    sum(output.get("answer_source") == "validated_renderer"
                        for output in answering_outputs) / len(answering_outputs)
                    if answering_outputs else None),
                "deterministic_fallback_rate": (
                    sum(output.get("answer_source") == "deterministic_claim_fallback"
                        for output in answering_outputs) / len(answering_outputs)
                    if answering_outputs else None),
                "missing_required_claim_conflicts": conflict_reasons.count(
                    "missing_required_claim"),
                "unsupported_statement_conflicts": conflict_reasons.count(
                    "unsupported_sentence"),
                "critical_value_conflicts": sum(reason in {
                    "critical_value_changed", "negation_or_status_changed"}
                    for reason in conflict_reasons),
                "rendering_diagnoses": sum(
                    report.get("failure_classification") == "RENDERING" for report in reports),
                "fact_selection_diagnoses": sum(
                    report.get("failure_classification") == "FACT_SELECTION" for report in reports),
                "candidate_rejections": sum(row["status"] == "rejected" for row in attempts),
                "planner_abstentions": sum(
                    row["status"] == "planner_abstention" for row in attempts),
                "escalations": len(escalation_queue),
                "escalation_rate": escalation_rate,
                "escalation_rate_warning": (
                    "high escalation rate" if incident_count and
                    escalation_rate >= config.ONLINE_ESCALATION_RATE_WARN else None),
                "reconsiderable_escalations": sorted(
                    item.get("incident_id") for item in escalation_queue
                    if item.get("reconsideration_eligible")),
                "escalated_incidents": [{
                    "incident_id": item.get("incident_id"),
                    "boundary_index": item.get("boundary_index"),
                    "safe_reason": item.get("reason_safe_proposal_not_produced"),
                    "reconsideration_eligible": bool(
                        item.get("reconsideration_eligible")),
                } for item in escalation_queue],
                "unresolved_incidents": [{
                    "incident_id": item.get("incident_id"),
                    "boundary_index": item.get("boundary_index"),
                    "phase": item.get("phase"),
                    "safe_reason": item.get("last_rejection_reason") or
                    item.get("bounded_reason"),
                } for item in incidents if item.get("incident_id") and
                    not item.get("resolved")],
                "interrupted_candidates": sum(row["status"] == "interrupted" for row in attempts),
                "unresolved_repair_incidents": sum(
                    bool(item.get("incident_id") and not item.get("resolved")) for item in incidents),
                "pending_repairs": len([item for item in pending if not item.get("resolved")
                                        and not item.get("terminal_outcome")
                                        and not item.get("abandoned")]),
                "unresolved_failures": sorted(item["checkpoint_id"] for item in pending
                                              if not item.get("resolved")),
                "abandoned_failures": sorted(item["checkpoint_id"] for item in pending
                                             if item.get("abandoned")),
                "terminal_unrepaired_failures": sorted(
                    item["checkpoint_id"] for item in pending
                    if item.get("terminal_outcome") in {
                        "unrepaired_after_5_attempts",
                        "partially_repaired_with_remaining_failures"}),
                "invalid_proposals": sum(row["status"] == "invalid_proposal"
                                         for row in attempts),
                "failed_candidate_attempts": sum(
                    bool(report.get("counts_as_failed_candidate")) for report in reports),
                "terminal_boundary_outcomes": {
                    str(item["boundary_index"]): item.get("resolution")
                    for item in incidents if item.get("resolution") in {
                        "unrepaired_after_5_attempts", "unrepresented_after_5_attempts",
                        "partially_repaired_with_remaining_failures", "escalated"}
                },
                "equivalent_attempt_rejections": sum(
                    report.get("rejection_reason") == "equivalent_previous_attempt"
                    for report in reports),
                "no_improvement_rejections": sum(
                    report.get("rejection_reason") in {
                        "no_measurable_improvement", "wrong_answer_no_targeted_improvement",
                        "cannot_represent_staged_data",
                    }
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
            "SELECT boundary,idx FROM attempts WHERE status IN ('promoted','partially_promoted')")}
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
            if state.get("boundary_outcome"):
                write_json(folder / "boundary_outcome.json", {
                    "boundary_index": row["idx"],
                    "boundary_outcome": state["boundary_outcome"],
                    "failed_candidate_attempts": int(
                        state.get("failed_candidate_attempts") or 0),
                    "invalid_proposal_count": int(
                        state.get("invalid_proposal_count") or 0),
                    "schedule_advanced": bool(state.get("schedule_advanced")),
                    "ingestion_occurred": bool(state.get("ingestion_occurred")),
                    "representation_committed": bool(
                        state.get("representation_committed")),
                    "representation_committed_version": state.get(
                        "representation_committed_version"),
                    "committed_system_version": state.get(
                        "committed_system_version", state.get("frozen_version")),
                    "staged_input_digest": state.get(
                        "staged_input_digest", state.get("delta_digest")),
                })
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
