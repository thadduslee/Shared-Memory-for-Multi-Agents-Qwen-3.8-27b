"""Allowlisted, content-free failure feedback for repair-model prompts."""
from __future__ import annotations

import hashlib
import json
from typing import Any

SAFE_FAILURE_CATEGORIES = frozenset({
    "infrastructure_or_inconclusive_vote",
    "authorization_denied_when_access_expected",
    "authorization_allowed_when_access_forbidden",
    "wrong_record_selected",
    "inactive_or_superseded_record_selected",
    "deleted_information_exposed",
    "insufficient_or_missing_evidence",
    "correct_evidence_but_wrong_action",
    "correct_evidence_but_answer_refused",
    "answer_unsupported_by_evidence",
    "malformed_or_invalid_output",
    "isolation_violation",
    "regression_of_previous_pass",
    "ingestion_or_state_reconstruction_failure",
    "nondeterministic_replay_result",
    "fact_selection_or_answer_plan_failure",
    "renderer_or_contract_failure",
    "judge_utility_failure_after_valid_grounded_render",
    "unknown_safe_failure_category",
})
_FEEDBACK_TRACE_ID_LIMIT = 16


def _hash(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    payload = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _vote_summary(result: dict[str, Any]) -> dict[str, int]:
    totals = {"unanimous_pass": 0, "majority_pass": 0, "unanimous_fail": 0,
              "majority_fail": 0, "inconclusive_or_error": 0}
    for pair in (result.get("pairs") or {}).values():
        for side in ("parent", "candidate"):
            vote = (pair.get(side) or {}).get("vote_summary") or {}
            outcome = vote.get("outcome")
            if outcome in totals:
                totals[outcome] += 1
    return totals


def classify_failure(report: dict[str, Any]) -> tuple[str, str]:
    """Classify the earliest trustworthy failing stage using safe observables."""
    stage = str(report.get("validation_stage") or report.get("failed_gate") or "")
    reason = str(report.get("rejection_reason") or "")
    targeted = report.get("targeted_replay") or {}
    vote = _vote_summary(targeted)
    if (report.get("candidate_failure_category") in {
            "infrastructure_failure", "prompt_context_failure", "budget_exhausted"} or
            report.get("bounded_or_inconclusive") or vote["inconclusive_or_error"]):
        return "infrastructure_or_inconclusive_vote", "INFRASTRUCTURE"
    if stage == "isolation" or report.get("gate_status", {}).get("episode_isolation") == "failed":
        return "isolation_violation", "ISOLATION"
    if report.get("trigger_type") == "pre_ingestion" or stage in {
        "data_preservation", "runtime_derivation", "derived_evidence",
    }:
        return "ingestion_or_state_reconstruction_failure", "INGESTION"
    if reason == "safety_regression":
        broad = report.get("broad_validation") or {}
        if broad.get("deletion_regressions"):
            return "deleted_information_exposed", "SAFETY"
        if broad.get("privacy_regressions"):
            return "authorization_allowed_when_access_forbidden", "AUTHORIZATION"
        return "regression_of_previous_pass", "SAFETY"
    if reason in {"diagnosis_review_rejected", "no_actionable_candidate_produced"}:
        return "malformed_or_invalid_output", "PLANNING"
    if reason in {"implementation_failure", "approach_unworkable",
                  "no_material_code_change", "duplicate_source_change"}:
        return "malformed_or_invalid_output", "BUILD"
    pairs = targeted.get("pairs") or {}
    candidates = [pair.get("candidate") or {} for pair in pairs.values()]
    # Action/authorization precedes retrieval and answer construction.
    for candidate in candidates:
        if candidate.get("authorized_evidence_count", 0) and candidate.get("action") in {
                "refuse", "no_memory"}:
            return "correct_evidence_but_answer_refused", "AUTHORIZATION"
    contract_reasons = {
        str(item) for candidate in candidates
        for item in (candidate.get("renderer_contract") or {}).get("reasons", [])}
    attempted_mismatch = any(
        bool((candidate.get("renderer_contract") or {}).get("attempted_action_mismatch"))
        for candidate in candidates)
    if "action_conflict" in contract_reasons or attempted_mismatch:
        return "correct_evidence_but_wrong_action", "RENDERING"
    if any(candidate.get("authorized_evidence_count", 0) and (
            not candidate.get("answer_plan_valid", True) or
            not candidate.get("required_claim_count", 0) or
            candidate.get("required_without_query_facets_count", 0) > 0)
           for candidate in candidates):
        return "fact_selection_or_answer_plan_failure", "FACT_SELECTION"
    if contract_reasons & {
            "malformed_renderer_output", "unknown_claim", "unsupported_sentence",
            "critical_value_changed", "value_role_changed", "negation_or_status_changed",
            "missing_required_claim", "unauthorized_record_reference"}:
        return "renderer_or_contract_failure", "RENDERING"
    # Authorization denials are distinguishable from a retrieval miss because
    # the trace exposes denied counts without releasing denied contents.
    if any(not candidate.get("authorized_evidence_count", 0) and
           candidate.get("denied_record_count", 0) for candidate in candidates):
        return "authorization_denied_when_access_expected", "AUTHORIZATION"
    if any(not candidate.get("authorized_evidence_count", 0) for candidate in candidates):
        return "insufficient_or_missing_evidence", "RETRIEVAL"
    if any(not candidate.get("passed", False) and
           not (candidate.get("renderer_contract") or {}).get("conflict") and
           candidate.get("answer_source") == "validated_renderer"
           for candidate in candidates):
        return "judge_utility_failure_after_valid_grounded_render", "JUDGE_UTILITY"
    if targeted.get("failed_checkpoint_ids"):
        return "unknown_safe_failure_category", "UNKNOWN"
    return "unknown_safe_failure_category", "UNKNOWN"


def categorize(report: dict[str, Any]) -> str:
    return classify_failure(report)[0]


def sanitize_build_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
    """Developer correction feedback: gate names and hashes, never raw output."""
    reports = feedback.get("gate_reports") or {}
    return {
        "format_version": 1,
        "stage": str(feedback.get("stage") or "build_validation"),
        "failure_category": "malformed_or_invalid_output",
        "error_signature": _hash(feedback.get("error")),
        "gates": {
            str(name): {
                "passed": bool(value.get("ok")),
                "diagnostic_signature": _hash(value),
            }
            for name, value in reports.items() if isinstance(value, dict)
        },
    }


def sanitized_failure_feedback(incident: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Produce the only candidate-failure object allowed into model prompts."""
    targeted = report.get("targeted_replay") or {}
    broad = report.get("broad_validation") or {}
    pairs = targeted.get("pairs") or {}
    selected = denied = invalid = deleted = active = 0
    behavior_changed = False
    for pair in pairs.values():
        candidate = pair.get("candidate") or {}
        selected += int(candidate.get("authorized_evidence_count") or 0)
        denied += int(candidate.get("denied_record_count") or 0)
        invalid += int(candidate.get("invalid_record_count") or 0)
        deleted += int(candidate.get("deleted_record_count") or 0)
        active += int(candidate.get("active_record_count") or 0)
        behavior_changed = behavior_changed or bool(
            pair.get("evidence_changed") or pair.get("answer_changed") or
            pair.get("decision_trace_changed") or pair.get("score_changed") or
            pair.get("selected_claims_changed") or pair.get("required_claims_changed") or
            pair.get("contract_result_changed") or pair.get("fallback_use_changed") or
            pair.get("answer_source_changed"))
    category, failure_stage = classify_failure(report)
    diagnostics = []
    for target in (incident.get("architect_incident_dossier") or {}).get("targets") or []:
        if not isinstance(target, dict):
            continue
        allowed_trace_ids = target.get("allowed_trace_ids") or (
            [target["trace_id"]] if target.get("trace_id") else [])
        diagnostics.append({
            "checkpoint_id": target.get("checkpoint_id"),
            "heuristic_action_hint": target.get("heuristic_action_hint"),
            "heuristic_action_hint_confidence": target.get(
                "heuristic_action_hint_confidence"),
            "heuristic_action_hint_derivation": target.get(
                "heuristic_action_hint_derivation"),
            "observed_action_category": target.get("observed_action_category") or
                (target.get("official_output") or {}).get("action"),
            "requester_role": target.get("requester_role"),
            "authorized_evidence_count": int(
                target.get("authorized_evidence_count") or 0),
            "denied_evidence_count": int(target.get("denied_evidence_count") or 0),
            "decision_rule_summaries": target.get("decision_rule_summaries") or [],
            "allowed_trace_ids": list(allowed_trace_ids)[:_FEEDBACK_TRACE_ID_LIMIT],
            "trace_count": len(allowed_trace_ids),
            "omitted_trace_id_count": max(
                0, len(allowed_trace_ids) - _FEEDBACK_TRACE_ID_LIMIT),
            "likely_problem": target.get("likely_problem") or (
                "INGESTION" if incident.get("trigger_type") == "pre_ingestion" else None),
        })
    feedback = {
        "format_version": 2,
        "incident_id": incident.get("incident_id"),
        "safe_parent_version": report.get("safe_parent_version"),
        "candidate_system_version": report.get("candidate_system_version"),
        "failure_category": category if category in SAFE_FAILURE_CATEGORIES else (
            "unknown_safe_failure_category"),
        "failure_stage": failure_stage,
        "pipeline_phase": str(report.get("phase") or "unknown"),
        "requested_action_type": "retrieve",
        "incident_diagnostics": diagnostics,
        "authorized_evidence_existed": selected > 0,
        "suspected_subsystems": {
            "retrieval": failure_stage == "RETRIEVAL",
            "authorization": failure_stage == "AUTHORIZATION",
            "ingestion": failure_stage == "INGESTION",
            "fact_selection": failure_stage == "FACT_SELECTION",
            "rendering": failure_stage == "RENDERING",
            "infrastructure": failure_stage == "INFRASTRUCTURE",
            "judge_utility": failure_stage == "JUDGE_UTILITY",
        },
        "record_counts": {"selected": selected, "denied": denied, "invalid": invalid,
                          "deleted": deleted, "active": active},
        "behavior_changed_from_parent": behavior_changed,
        "improved_targets": sorted(targeted.get("failing_to_passing") or []),
        "remaining_targets": sorted(targeted.get("failed_checkpoint_ids") or []),
        "regressed_targets": sorted(set(
            (targeted.get("passing_to_failing") or []) +
            (broad.get("passing_to_failing") or []))),
        "previously_passing_target_regressed": bool(
            targeted.get("passing_to_failing") or broad.get("passing_to_failing")),
        "vote_summary": _vote_summary(targeted),
        "build": {
            "status": (report.get("build_validation") or {}).get("status", "not_run"),
            "diagnostic_signature": _hash(report.get("build_test_errors")),
        },
        "rejection_code": str(report.get("rejection_reason") or "unknown"),
        "rejection_reason": str(report.get("rejection_reason") or "unknown"),
        "targeted_replay": {
            "status": targeted.get("status", "not_run"),
            "evidence_changed": targeted.get("evidence_changed"),
            "decision_trace_changed": targeted.get("decision_trace_changed"),
            "answer_changed": targeted.get("answer_changed"),
            "score_changed": targeted.get("score_changed"),
            "selected_claims_changed": targeted.get("selected_claims_changed"),
            "required_claims_changed": targeted.get("required_claims_changed"),
            "contract_result_changed": targeted.get("contract_result_changed"),
            "contract_reasons_before": targeted.get("contract_reasons_before") or [],
            "contract_reasons_after": targeted.get("contract_reasons_after") or [],
            "fallback_use_changed": targeted.get("fallback_use_changed"),
            "answer_source_changed": targeted.get("answer_source_changed"),
            "intermediate_contract_problem_disappeared": targeted.get(
                "intermediate_contract_problem_disappeared"),
            "final_result_changed": targeted.get("final_result_changed"),
            "parent_vote_summary": targeted.get("parent_vote_summary") or {},
            "candidate_vote_summary": targeted.get("candidate_vote_summary") or {},
            "replay_unchanged": targeted.get("replay_unchanged"),
        },
        "broad_validation": {
            "status": broad.get("status", "not_run"),
            "regression_count": len(broad.get("regressions") or []),
        },
        "failure_classification": {
            "safety_regression": report.get("rejection_reason") == "safety_regression",
            "implementation_failure": report.get("candidate_failure_category") == (
                "implementation_build_failure"),
            "ineffective_design": report.get("rejection_reason") in {
                "no_measurable_improvement", "wrong_answer_no_targeted_improvement"},
            "duplicate_proposal": report.get("rejection_reason") == (
                "equivalent_previous_attempt"),
            "diagnosis_review_rejection": report.get("rejection_reason") == (
                "diagnosis_review_rejected"),
            "tests_never_ran": sorted(
                key for key in ("compile", "invariants", "unit_tests")
                if report.get("gate_status", {}).get(key) == "not_run"),
        },
        "regressions": {
            "checkpoint_transitions": sorted(set(
                (targeted.get("passing_to_failing") or []) +
                (broad.get("passing_to_failing") or []))),
            "preservation_failed": bool(report.get("protected_state_difference")),
            "preservation_signature": _hash(report.get("protected_state_difference")),
            "preservation": _hash(report.get("protected_state_difference")),
            "isolation": report.get("gate_status", {}).get("episode_isolation") == "failed",
        },
        "remaining_failed_checkpoints": sorted(targeted.get("failed_checkpoint_ids") or []),
        "diagnosis_signature": report.get("diagnosis_signature"),
        "design_signature": report.get("design_signature"),
        "source_diff_signature": report.get("source_diff_signature"),
        "behavioral_signature": report.get("behavioral_signature"),
        "counts_as_failed_candidate": bool(report.get("counts_as_failed_candidate")),
        "failed_candidate_attempts_after": report.get("failed_candidate_attempts_after"),
    }
    # Defense in depth: serializing this object must not depend on any raw
    # evaluator/build field.  Every leaf above is locally constructed.
    return feedback


def compact_failure_feedback(feedback: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bound legacy and current trace lists before feedback enters a prompt."""
    if not isinstance(feedback, dict):
        return None
    diagnostics = []
    for raw in feedback.get("incident_diagnostics") or []:
        if not isinstance(raw, dict):
            continue
        trace_ids = list(raw.get("allowed_trace_ids") or [])
        diagnostics.append({
            **raw,
            "allowed_trace_ids": trace_ids[:_FEEDBACK_TRACE_ID_LIMIT],
            "trace_count": int(raw.get("trace_count") or len(trace_ids)),
            "omitted_trace_id_count": max(
                int(raw.get("omitted_trace_id_count") or 0),
                len(trace_ids) - _FEEDBACK_TRACE_ID_LIMIT,
            ),
        })
    return {**feedback, "incident_diagnostics": diagnostics}
