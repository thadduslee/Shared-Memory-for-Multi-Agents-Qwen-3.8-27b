"""Deterministic, privacy-minimized diagnostics for online repair incidents."""
from __future__ import annotations

import re
from typing import Any

from gatemem_adapter import assert_no_hidden_fields, visible_checkpoint

_TRACE_FIELDS = {
    "trace_id", "record_id", "source_observation_id", "status", "classification",
    "structural_concepts", "outcome", "decision_stage", "decision_rule",
    "permission_ids", "permission_validity_at_query",
}
_PERMISSION_FIELDS = {"permission_id", "status", "effect", "valid_at_query"}
_EVIDENCE_FIELDS = {"record_id", "source_observation_id", "text", "status", "role"}
_ARCHITECT_TRACE_EXAMPLE_LIMIT = 16


def _causal_route(*, authorized_count: int, denied_count: int,
                  claims: list[dict[str, Any]], contract: dict[str, Any],
                  observed_action: Any, judge_passed: bool) -> tuple[str, str]:
    """Return the first failed stage and a safe reason enum."""
    if authorized_count and observed_action in {"refuse", "no_memory"}:
        return "AUTHORIZATION", "trusted_action_denied_with_authorized_evidence"
    if not authorized_count:
        return (("AUTHORIZATION", "responsive_records_denied") if denied_count else
                ("RETRIEVAL", "no_authorized_evidence_retrieved"))
    required = [claim for claim in claims if claim.get("required")]
    if not claims or not required:
        return "FACT_SELECTION", "missing_or_empty_answer_plan"
    if any(claim.get("required") and not claim.get("query_facets") for claim in claims):
        return "FACT_SELECTION", "required_claim_lacks_visible_query_facet"
    reasons = {str(item) for item in contract.get("reasons") or []}
    if contract.get("conflict") or reasons:
        return "RENDERING", sorted(reasons)[0] if reasons else "renderer_contract_conflict"
    if not judge_passed:
        return "UNKNOWN/ESCALATE", "grounded_render_rejected_for_utility"
    return "UNKNOWN/ESCALATE", "no_visible_failure_stage"


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _allow(item: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: item.get(key) for key in sorted(fields) if key in item}


def _safe_trace_item(item: dict[str, Any]) -> dict[str, Any]:
    result = _allow(item, _TRACE_FIELDS)
    token = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$", re.IGNORECASE)
    for key in ("status", "classification", "outcome", "decision_stage", "decision_rule"):
        value = str(result.get(key) or "unknown")
        result[key] = value if token.fullmatch(value) else "unknown"
    result["structural_concepts"] = sorted({
        str(value) for value in (result.get("structural_concepts") or [])
        if re.fullmatch(r"[a-z0-9_]+_expression", str(value), re.IGNORECASE)
    })
    validity = result.get("permission_validity_at_query")
    if isinstance(validity, dict):
        result["permission_validity_at_query"] = {
            "query_time_present": bool(validity.get("query_time")),
            "active_permission_ids": list(validity.get("active_permission_ids") or []),
        }
    result["permission_ids"] = list(result.get("permission_ids") or [])
    return result


def safe_decision_trace(output: dict[str, Any]) -> list[dict[str, Any]]:
    """Allowlist trace metadata; never copy an untrusted debug object wholesale."""
    debug = output.get("debug") if isinstance(output.get("debug"), dict) else {}
    result = [_safe_trace_item(item) for item in _objects(debug.get("decision_trace"))]
    if not result:
        evidence = output.get("retrieved_memory") or output.get("evidence") or []
        for position, item in enumerate(_objects(evidence), 1):
            record_id = item.get("record_id")
            result.append({
                "trace_id": f"released:{record_id or position}",
                "record_id": record_id,
                "source_observation_id": item.get("source_observation_id"),
                "status": item.get("status", "unknown"),
                "classification": "released_evidence",
                "structural_concepts": [],
                "outcome": "selected",
                "decision_stage": "release",
                "decision_rule": "authorized_evidence_release",
                "permission_ids": [],
            })
    if not result:
        result.append({
            "trace_id": "output:decision",
            "record_id": None,
            "source_observation_id": None,
            "status": "not_applicable",
            "classification": "official_output",
            "structural_concepts": [],
            "outcome": str(output.get("action") or "unknown"),
            "decision_stage": "output_composition",
            "decision_rule": "official_output_composition",
            "permission_ids": [],
        })
    return sorted(result, key=lambda item: (
        str(item.get("trace_id") or ""), str(item.get("record_id") or "")))


def safe_authorized_evidence(output: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = output.get("retrieved_memory") or output.get("evidence") or []
    return [_allow(item, _EVIDENCE_FIELDS) for item in _objects(evidence)]


def build_incident_dossier(incident_id: str, boundary_index: int,
                           official_rows: list[dict[str, Any]],
                           checkpoints: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a causal artifact exclusively from boundary-visible prediction data."""
    targets = []
    all_trace_ids: list[str] = []
    for row in sorted(official_rows, key=lambda item: item["checkpoint_id"]):
        checkpoint = visible_checkpoint(checkpoints[row["checkpoint_id"]])
        output = row.get("prediction", {}).get("output", {})
        if not isinstance(output, dict):
            output = {}
        debug = output.get("debug") if isinstance(output.get("debug"), dict) else {}
        trace = safe_decision_trace(output)
        for item in trace:
            trace_id = str(item.get("trace_id") or "")
            if trace_id.startswith(("released:", "output:")):
                item["trace_id"] = f"{row['checkpoint_id']}:{trace_id}"
        for item in trace:
            if item.get("trace_id"):
                all_trace_ids.append(str(item["trace_id"]))
        permissions = [_allow(item, _PERMISSION_FIELDS)
                       for item in _objects(debug.get("permission_facts"))]
        denied_count = int(debug.get("n_denied_rbac") or 0) + int(
            debug.get("n_denied_scope") or 0)
        authorized_count = len(safe_authorized_evidence(output))
        authorized_ids = {str(item.get("record_id")) for item in safe_authorized_evidence(output)
                          if item.get("record_id")}
        plan = output.get("answer_plan") if isinstance(output.get("answer_plan"), dict) else {}
        safe_claims = [{
            "claim_id": str(claim.get("claim_id") or ""),
            "fact": str(claim.get("fact") or ""),
            "required": bool(claim.get("required")),
            "source_record_id": str(claim.get("source_record_id") or ""),
            "source_observation_id": claim.get("source_observation_id"),
            "query_facets": list(claim.get("query_facets") or []),
            "critical_values": list(claim.get("critical_values") or []),
        } for claim in _objects(plan.get("claims"))
            if str(claim.get("source_record_id") or "") in authorized_ids]
        contract = (output.get("renderer_contract")
                    if isinstance(output.get("renderer_contract"), dict) else {})
        observed_action = output.get("action")
        # This is inferred only from boundary-visible authorization behavior.  It
        # is deliberately not copied from GateMem's hidden `expected_action`.
        heuristic_action = (
            "answer_redacted" if authorized_count and denied_count else
            "answer" if authorized_count else
            "refuse" if denied_count else None
        )
        heuristic_confidence = "medium" if authorized_count or denied_count else "low"
        for reason in sorted({str(item) for item in contract.get("reasons") or []}):
            contract_trace = {
                "trace_id": f"{row['checkpoint_id']}:renderer_contract:{reason}",
                "record_id": None,
                "source_observation_id": None,
                "status": "not_applicable",
                "classification": "renderer_contract",
                "structural_concepts": [],
                "outcome": "failed",
                "decision_stage": "renderer_contract",
                "decision_rule": reason,
                "permission_ids": [],
            }
            trace.append(contract_trace)
            all_trace_ids.append(contract_trace["trace_id"])
        rule_counts: dict[tuple[str, str, str], int] = {}
        for item in trace:
            key = (
                str(item.get("decision_stage") or "unknown"),
                str(item.get("decision_rule") or "unknown"),
                str(item.get("outcome") or "unknown"),
            )
            rule_counts[key] = rule_counts.get(key, 0) + 1
        rule_summary = [{
            "stage": stage, "rule": rule, "outcome": outcome, "count": count,
        } for (stage, rule, outcome), count in sorted(rule_counts.items())]
        likely_problem, causal_reason = _causal_route(
            authorized_count=authorized_count, denied_count=denied_count,
            claims=safe_claims, contract=contract, observed_action=observed_action,
            judge_passed=bool(row.get("score", {}).get("passed")))
        targets.append({
            "checkpoint_id": row["checkpoint_id"],
            "visible_query": checkpoint.get("query_text", ""),
            "requested_action_type": "retrieve",
            "requester_present": bool((checkpoint.get("asker") or {}).get("principal_id")),
            "requester_role": (checkpoint.get("asker") or {}).get("role"),
            "heuristic_action_hint": heuristic_action,
            "heuristic_action_hint_confidence": heuristic_confidence,
            "heuristic_action_hint_derivation": "visible_authorized_and_denied_counts",
            "observed_action_category": observed_action,
            "official_output": {
                "action": observed_action,
            },
            "authorized_evidence_count": authorized_count,
            "retrieved_authorized_record_ids": sorted(authorized_ids),
            "selected_claim_ids": [item["claim_id"] for item in safe_claims],
            "required_claim_ids": [item["claim_id"] for item in safe_claims if item["required"]],
            "authorized_selected_claims": safe_claims,
            "renderer_input": {"action": observed_action, "claims": safe_claims},
            "renderer_output": output.get("renderer_attempt"),
            "renderer_contract_result": contract,
            "fallback_used": output.get("answer_source") == "deterministic_claim_fallback",
            "final_released_answer": str(output.get("answer") or ""),
            "judge_passed": bool(row.get("score", {}).get("passed")),
            "version_tuple": {
                "memory_version": output.get("memory_version"),
                "renderer_version": output.get("renderer_version"),
                "contract_version": output.get("contract_version"),
                "renderer_mode": output.get("renderer_mode"),
            },
            "denied_evidence_count": denied_count,
            "decision_rule_summaries": rule_summary,
            "allowed_trace_ids": sorted(
                str(item["trace_id"]) for item in trace if item.get("trace_id")),
            "likely_problem": likely_problem,
            "causal_reason": causal_reason,
            "decision_trace": trace,
            "permission_validity_at_query": permissions,
            "query_facet_count": len(debug.get("query_facets") or []),
            "supported_query_facet_count": len(debug.get("supported_query_facets") or []),
            "unsupported_query_facet_count": len(debug.get("unsupported_query_facets") or []),
            "decision_timeline": [{
                "sequence": sequence,
                "trace_id": item.get("trace_id"),
                "stage": item.get("decision_stage"),
                "outcome": item.get("outcome"),
                "rule": item.get("decision_rule"),
            } for sequence, item in enumerate(trace, 1)],
        })
    dossier = {
        "format_version": 1,
        "incident_id": incident_id,
        "boundary_index": boundary_index,
        "causal_visibility": "boundary_visible_only",
        "targets": targets,
        "trace_entry_ids": sorted(set(all_trace_ids)),
    }
    assert_no_hidden_fields(dossier, where="architect incident dossier")
    return dossier


def compact_incident_dossier(dossier: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, citation-valid Architect view of a durable dossier.

    The durable artifact intentionally retains every safe trace.  Sending the
    same trace IDs again in ``trace_entry_ids``, ``allowed_trace_ids``, the full
    trace, and a timeline made one 95-record incident consume most of a 65k
    context window.  The Architect needs aggregate rule counts plus a small set
    of real trace IDs it can cite; deterministic diagnosis review still checks
    those citations against the full durable dossier.
    """
    source = dossier if isinstance(dossier, dict) else {}
    targets = []
    trace_entry_ids: list[str] = []
    for raw_target in _objects(source.get("targets")):
        trace = _objects(raw_target.get("decision_trace"))
        if not trace and raw_target.get("trace_id"):
            trace = [_safe_trace_item(raw_target)]
        representatives: list[dict[str, Any]] = []
        seen_rules: set[tuple[str, str, str]] = set()
        for item in trace:
            key = (
                str(item.get("decision_stage") or "unknown"),
                str(item.get("decision_rule") or "unknown"),
                str(item.get("outcome") or "unknown"),
            )
            if key in seen_rules:
                continue
            seen_rules.add(key)
            representatives.append(item)
            trace_id = str(item.get("trace_id") or "")
            if trace_id:
                trace_entry_ids.append(trace_id)
            if len(representatives) >= _ARCHITECT_TRACE_EXAMPLE_LIMIT:
                break

        permission_counts: dict[tuple[str, str, bool], int] = {}
        for item in _objects(raw_target.get("permission_validity_at_query")):
            key = (
                str(item.get("status") or "unknown"),
                str(item.get("effect") or "unknown"),
                bool(item.get("valid_at_query")),
            )
            permission_counts[key] = permission_counts.get(key, 0) + 1

        target = {
            key: raw_target.get(key)
            for key in (
                "checkpoint_id", "requested_action_type", "requester_present",
                "requester_role", "heuristic_action_hint",
                "heuristic_action_hint_confidence", "heuristic_action_hint_derivation",
                "observed_action_category", "official_output",
                "authorized_evidence_count", "denied_evidence_count",
                "decision_rule_summaries", "likely_problem", "causal_reason", "query_facet_count",
                "supported_query_facet_count", "unsupported_query_facet_count",
                "structural_summary", "error_present", "error_signature",
                "visible_query", "retrieved_authorized_record_ids", "selected_claim_ids",
                "required_claim_ids", "authorized_selected_claims", "renderer_input",
                "renderer_output", "renderer_contract_result", "fallback_used",
                "final_released_answer", "judge_passed", "version_tuple",
            )
            if key in raw_target
        }
        target.update({
            "decision_trace": representatives,
            "trace_count": len(trace),
            "omitted_trace_count": max(0, len(trace) - len(representatives)),
            "permission_fact_summaries": [
                {"status": status, "effect": effect, "valid_at_query": valid,
                 "count": count}
                for (status, effect, valid), count in sorted(permission_counts.items())
            ],
        })
        targets.append(target)

    compact = {
        "format_version": source.get("format_version", 1),
        "incident_id": source.get("incident_id"),
        "boundary_index": source.get("boundary_index"),
        "causal_visibility": source.get("causal_visibility"),
        "targets": targets,
        "trace_entry_ids": sorted(set(trace_entry_ids)),
        "full_trace_count": sum(int(target.get("trace_count") or 0) for target in targets),
        "prompt_view": "compact_representative_traces",
    }
    assert_no_hidden_fields(compact, where="compact architect incident dossier")
    return compact


def replay_behavior(output: dict[str, Any]) -> dict[str, Any]:
    """The safe portion of a replay prediction used in Architect feedback."""
    import hashlib

    evidence = safe_authorized_evidence(output)
    safe_evidence = [{key: item.get(key) for key in (
        "record_id", "source_observation_id", "status") if key in item}
        for item in evidence]
    debug = output.get("debug") if isinstance(output.get("debug"), dict) else {}
    answer = " ".join(str(output.get("answer") or "").split())
    plan = output.get("answer_plan") if isinstance(output.get("answer_plan"), dict) else {}
    claims = _objects(plan.get("claims"))
    required = [claim for claim in claims if claim.get("required")]
    return {
        "action": output.get("action"),
        "rendered_answer_hash": hashlib.sha256(answer.encode()).hexdigest(),
        "selected_claim_ids": list(output.get("selected_claim_ids") or []),
        "required_claim_ids": list(output.get("required_claim_ids") or []),
        "answer_plan_valid": bool(claims) and bool(required),
        "selected_claim_count": len(claims),
        "required_claim_count": len(required),
        "optional_claim_count": len(claims) - len(required),
        "required_without_query_facets_count": sum(
            1 for claim in required if not claim.get("query_facets")),
        "renderer_contract": output.get("renderer_contract") or {},
        "fallback_used": output.get("answer_source") == "deterministic_claim_fallback",
        "answer_source": output.get("answer_source"),
        "memory_version": output.get("memory_version"),
        "renderer_version": output.get("renderer_version"),
        "contract_version": output.get("contract_version"),
        "authorized_evidence": safe_evidence,
        "authorized_evidence_count": len(safe_evidence),
        "denied_record_count": int(debug.get("n_denied_rbac") or 0) + int(
            debug.get("n_denied_scope") or 0),
        "deleted_record_count": int(debug.get("n_denied_tombstone") or 0),
        "invalid_record_count": int(debug.get("n_obsolete") or 0),
        "active_record_count": len(safe_evidence),
        "renderer_conflict": bool(output.get("renderer_conflict")),
        "decision_trace": safe_decision_trace(output),
    }
