"""Deterministic, privacy-minimized diagnostics for online repair incidents."""
from __future__ import annotations

import re
from typing import Any

from gatemem_adapter import assert_no_hidden_fields, visible_checkpoint


_TRACE_FIELDS = {
    "trace_id", "record_id", "source_observation_id", "status", "classification",
    "structural_concepts", "event_time_features", "outcome", "decision_stage",
    "decision_rule", "permission_ids", "permission_validity_at_query",
    "record_valid_from", "record_valid_until",
}
_PERMISSION_FIELDS = {
    "permission_id", "source_observation_id", "status", "effect", "scope",
    "exclusions", "valid_from", "valid_until", "valid_at_query", "query_time",
}
_EVIDENCE_FIELDS = {"record_id", "source_observation_id", "text", "status", "role"}


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _allow(item: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: item.get(key) for key in sorted(fields) if key in item}


def _safe_trace_item(item: dict[str, Any]) -> dict[str, Any]:
    result = _allow(item, _TRACE_FIELDS)
    token = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$", re.I)
    for key in ("status", "classification", "outcome", "decision_stage", "decision_rule"):
        value = str(result.get(key) or "unknown")
        result[key] = value if token.fullmatch(value) else "unknown"
    result["structural_concepts"] = sorted({
        str(value) for value in (result.get("structural_concepts") or [])
        if re.fullmatch(r"[a-z0-9_]+_expression", str(value), re.I)
    })
    temporal = re.compile(
        r"^(?:\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(?:\s*(?:am|pm))?|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)$", re.I)
    result["event_time_features"] = sorted({
        str(value).lower() for value in (result.get("event_time_features") or [])
        if temporal.fullmatch(str(value))
    })
    validity = result.get("permission_validity_at_query")
    if isinstance(validity, dict):
        result["permission_validity_at_query"] = {
            "query_time": validity.get("query_time"),
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
                "event_time_features": [],
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
            "event_time_features": [],
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
        targets.append({
            "checkpoint_id": row["checkpoint_id"],
            "visible_query": checkpoint,
            "requester": checkpoint.get("asker") or {},
            "official_output": {
                "action": output.get("action"),
                "rendered_answer": output.get("answer", ""),
            },
            "authorized_evidence_released": safe_authorized_evidence(output),
            "decision_trace": trace,
            "permission_validity_at_query": permissions,
            "query_facets": list(debug.get("query_facets") or []),
            "supported_query_facets": list(debug.get("supported_query_facets") or []),
            "unsupported_query_facets": list(debug.get("unsupported_query_facets") or []),
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


def replay_behavior(output: dict[str, Any]) -> dict[str, Any]:
    """The safe portion of a replay prediction used in Architect feedback."""
    return {
        "action": output.get("action"),
        "rendered_answer": str(output.get("answer") or ""),
        "authorized_evidence": safe_authorized_evidence(output),
        "decision_trace": safe_decision_trace(output),
    }
