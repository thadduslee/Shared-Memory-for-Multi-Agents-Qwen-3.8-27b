"""Grounded answer-plan, renderer contract, and component ownership tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from online.backend import Backend
from online.runner import OnlineOptions, OnlineRunner
from online.validation import ValidationFailure
from renderer_contract import (
    TrustedRenderDecision, enforce_render_contract, extract_critical_values,
)
from templates.memory_system.agent import GateMemAgent, _answer_plan
from templates.memory_system.store import Decision, Evidence


def _record(fact="The appointment is Tuesday at 9:00 AM.", *, critical=None):
    claim = {
        "claim_id": "claim_one", "fact": fact, "required": True,
        "source_record_id": "record_one", "source_observation_id": "observation_one",
        "query_facets": ["appointment"],
        "critical_values": critical or [],
    }
    return {"action": "answer", "answer_plan": {"schema_version": 1, "claims": [claim]},
            "fallback_answer": fact,
            "evidence": [{"record_id": "record_one", "source_observation_id": "observation_one",
                          "text": fact, "status": "active"}]}


def _render(text, ids=None, **extra):
    ids = ids if ids is not None else ["claim_one"]
    return {"sentences": [{"text": text, "claim_ids": ids}], **extra}


def test_exact_required_claim_is_released_from_validated_renderer():
    decision = TrustedRenderDecision.from_record(_record())
    output, result = enforce_render_contract(
        decision, _render("The appointment is Tuesday at 9:00 AM."))
    assert not result["conflict"]
    assert output["answer_source"] == "validated_renderer"


@pytest.mark.parametrize(("proposal", "reason"), [
    ({"action": "answer", "sentences": [], "used_claim_ids": []},
     "malformed_renderer_output"),
    (_render("The appointment is Tuesday at 9:00 AM.", ["unknown"]), "unknown_claim"),
    ({"sentences": [{"text": "The appointment is Tuesday at 9:00 AM.",
                      "claim_ids": "claim_one"}]}, "malformed_renderer_output"),
    (_render("The appointment is Tuesday at 9:00 AM. Bring an umbrella."),
     "unsupported_sentence"),
])
def test_invalid_renderer_output_falls_back(proposal, reason):
    output, result = enforce_render_contract(
        TrustedRenderDecision.from_record(_record()), proposal)
    assert result["conflict"] and reason in result["reasons"]
    assert output["answer_source"] == "deterministic_claim_fallback"
    assert output["answer"] == _record()["fallback_answer"]


def test_renderer_action_is_ignored_and_trusted_action_is_inserted():
    decision = TrustedRenderDecision.from_record(_record())
    output, result = enforce_render_contract(
        decision, {**_render("The appointment is Tuesday at 9:00 AM."),
                   "action": "refuse"})
    assert not result["conflict"]
    assert result["attempted_action_mismatch"] is True
    assert output["action"] == "answer"

    _, missing = enforce_render_contract(
        decision, {"sentences": [{"text": "Tuesday.",
                                                        "claim_ids": []}],
                   })
    assert "missing_required_claim" in missing["reasons"]


def test_time_role_status_and_negation_changes_are_rejected():
    time_record = _record(critical=[
        {"type": "time", "canonical_value": "9:00 am", "surface_value": "9:00 AM"},
        {"type": "date", "canonical_value": "tuesday", "surface_value": "Tuesday"},
    ])
    _, changed = enforce_render_contract(
        TrustedRenderDecision.from_record(time_record),
        _render("The appointment is Tuesday at 10:00 AM."))
    assert "critical_value_changed" in changed["reasons"]

    arrival = _record("Please arrive at 8:30 AM.", critical=[
        {"type": "time", "canonical_value": "8:30 am", "surface_value": "8:30 AM"}])
    _, confused = enforce_render_contract(
        TrustedRenderDecision.from_record(arrival),
        _render("The appointment is at 8:30 AM."))
    assert "value_role_changed" in confused["reasons"]

    cancelled = _record("The visit is cancelled and is not scheduled.", critical=[
        {"type": "status", "canonical_value": "cancelled", "surface_value": "cancelled"},
        {"type": "negation", "canonical_value": "not", "surface_value": "not"},
    ])
    _, status = enforce_render_contract(
        TrustedRenderDecision.from_record(cancelled),
        _render("The visit is scheduled."))
    assert "negation_or_status_changed" in status["reasons"]


def test_value_cannot_be_reassigned_between_cited_claims():
    record = _record("Alex starts at 09:00.", critical=[
        {"type": "time", "canonical_value": "09:00", "surface_value": "09:00"}])
    record["evidence"].append({
        "record_id": "record_two", "source_observation_id": "observation_two",
        "text": "Jordan starts at 10:00.", "status": "active"})
    record["answer_plan"]["claims"].append({
        "claim_id": "claim_two", "fact": "Jordan starts at 10:00.", "required": True,
        "source_record_id": "record_two", "source_observation_id": "observation_two",
        "query_facets": ["starts"], "critical_values": [
            {"type": "time", "canonical_value": "10:00", "surface_value": "10:00"}]})
    decision = TrustedRenderDecision.from_record(record)
    output, result = enforce_render_contract(decision, {
        "action": "answer",
        "sentences": [{"text": "Alex starts at 10:00 and Jordan starts at 09:00.",
                       "claim_ids": ["claim_one", "claim_two"]}],
        "used_claim_ids": ["claim_one", "claim_two"],
    })
    assert "critical_value_changed" in result["reasons"]
    assert output["answer_source"] == "deterministic_claim_fallback"


def test_empty_authorized_answer_fails_closed():
    decision = TrustedRenderDecision.from_record({
        "action": "answer", "answer_plan": {"schema_version": 1, "claims": []},
        "evidence": [], "fallback_answer": ""})
    output, result = enforce_render_contract(decision, None)
    assert "empty_authorized_answer" in result["reasons"]
    assert output["answer"] == ""


def test_unauthorized_plan_claim_never_reaches_fallback():
    record = _record()
    record["answer_plan"]["claims"].append({
        "claim_id": "bad", "fact": "private extra", "required": True,
        "source_record_id": "denied", "critical_values": [], "query_facets": []})
    output, result = enforce_render_contract(
        TrustedRenderDecision.from_record(record), None)
    assert "unauthorized_record_reference" in result["reasons"]
    assert "private extra" not in output["answer"]


def test_agent_emits_authorized_required_plan_and_compatibility_alias():
    episode = {"episode_id": "episode", "domain": "generic", "entities": {
        "principals": [{"principal_id": "owner", "role": "member",
                        "display_name": "Owner Person"}], "relationships": []}}
    agent = GateMemAgent(":memory:")
    agent.reset(episode)
    agent.ingest({"turn_id": "t1", "turn_kind": "message",
                  "speaker": {"principal_id": "owner", "role": "member"},
                  "text": "The project review is Friday at 14:30."})
    visible = {"checkpoint_id": "visible", "episode_id": "episode",
               "as_of_turn_id": "t1", "asker": {"principal_id": "owner", "role": "member"},
               "query_text": "When is the project review?"}
    result = agent.query({**visible, "expected_action": "refuse",
                          "gold_answer_structured": {"hidden": True}})
    plain = agent.query(visible)
    assert result["answer_plan"] == plain["answer_plan"]
    assert result["fallback_answer"] == result["answer"]
    assert result["answer_compatibility_alias"] is True
    assert all(claim["required"] for claim in result["answer_plan"]["claims"])
    assert {claim["source_record_id"] for claim in result["answer_plan"]["claims"]} <= set(
        result["used_record_ids"])


def test_query_conditioned_plan_keeps_unrelated_authorized_context_optional():
    decision = Decision(
        query_terms=["appointment", "time_expression"],
        allowed=[
            Evidence("appointment", "t1", "member", "message", "scoped",
                     "The appointment starts at 09:00.", source_observation_id="o1"),
            Evidence("arrival", "t1", "member", "message", "scoped",
                     "Please arrive at 08:30.", source_observation_id="o1"),
            Evidence("history", "t2", "member", "message", "scoped",
                     "An earlier unrelated note remains active.", source_observation_id="o2"),
        ],
    )
    plan = _answer_plan(decision, "What time is the appointment?")
    required = [claim for claim in plan["claims"] if claim["required"]]
    optional = [claim for claim in plan["claims"] if not claim["required"]]
    assert [claim["source_record_id"] for claim in required] == ["appointment"]
    assert {claim["source_record_id"] for claim in optional} == {"arrival", "history"}
    record = {"action": "answer", "answer_plan": plan, "evidence": [
        {"record_id": item.record_id, "text": item.body, "status": "active"}
        for item in decision.allowed]}
    output, _ = enforce_render_contract(TrustedRenderDecision.from_record(record), None)
    assert "09:00" in output["answer"]
    assert "08:30" not in output["answer"] and "unrelated" not in output["answer"]


def test_claims_without_visible_query_facets_are_not_promoted_to_required():
    decision = Decision(
        query_terms=["unmatched_query_facet"],
        allowed=[Evidence(
            "history", "t1", "member", "message", "scoped",
            "An unrelated active note.", source_observation_id="o1")],
    )
    plan = _answer_plan(decision, "Please help with something else.")
    assert len(plan["claims"]) == 1
    assert plan["claims"][0]["query_facets"] == []
    assert plan["claims"][0]["required"] is False

    record = {"action": "answer", "answer_plan": plan, "evidence": [
        {"record_id": "history", "text": "An unrelated active note.", "status": "active"}]}
    output, result = enforce_render_contract(TrustedRenderDecision.from_record(record), None)
    assert output["answer"] == ""
    assert "empty_authorized_answer" in result["reasons"]


def test_used_ids_are_derived_from_sentence_attribution_not_model_bookkeeping():
    decision = TrustedRenderDecision.from_record(_record())
    output, result = enforce_render_contract(decision, {
        "sentences": [{"text": "The appointment is Tuesday at 9:00 AM.",
                       "claim_ids": ["claim_one"]}],
        "used_claim_ids": ["fabricated"], "used_record_ids": ["foreign"],
    })
    assert not result["conflict"]
    assert output["answer_structured"]["used_claim_ids"] == ["claim_one"]
    assert output["used_record_ids"] == ["record_one"]


def test_critical_value_extraction_is_typed_complete_and_non_overlapping():
    values = extract_critical_values(
        "On October 17, 2026 at 09:30 call +65 6123 4567 and take 1,250.5 mg "
        "for 3 days at 82.5%; ask Dr. Rowan Lee. The visit is not cancelled.")
    typed = {(value["type"], value["canonical_value"]) for value in values}
    assert ("date", "october 17, 2026") in typed
    assert ("time", "09:30") in typed
    assert ("phone_number", "+65 6123 4567") in typed
    assert ("dose", "1,250.5 mg") in typed
    assert ("quantity", "3 days") in typed
    assert ("percentage", "82.5%") in typed
    assert ("person", "dr. rowan lee") in typed
    assert ("negation", "not") in typed and ("status", "cancelled") in typed
    assert not any(value["type"] == "number" for value in values)


def test_safe_connective_wording_passes_but_subject_reversal_fails():
    decision = TrustedRenderDecision.from_record(_record())
    output, result = enforce_render_contract(
        decision, _render("According to the record, the appointment is Tuesday at 9:00 AM."))
    assert not result["conflict"] and output["answer_source"] == "validated_renderer"

    record = _record("Dr. Alex Reed called Dr. Jordan Cole.", critical=[
        {"type": "person", "role": "person", "canonical_value": "dr. alex reed",
         "surface_value": "Dr. Alex Reed"},
        {"type": "person", "role": "person", "canonical_value": "dr. jordan cole",
         "surface_value": "Dr. Jordan Cole"},
    ])
    _, reversed_result = enforce_render_contract(
        TrustedRenderDecision.from_record(record),
        _render("Dr. Jordan Cole called Dr. Alex Reed."))
    assert "critical_value_changed" in reversed_result["reasons"]


def test_current_malformed_plan_fails_while_absent_legacy_plan_is_reconstructed():
    current = TrustedRenderDecision.from_record({
        "action": "answer", "answer_plan": {"schema_version": 2, "claims": []},
        "evidence": [{"record_id": "r1", "text": "Authorized fact.", "status": "active"}],
    })
    current_output, current_result = enforce_render_contract(current, None)
    assert "invalid_answer_plan" in current_result["reasons"]
    assert current_output["answer"] == ""

    legacy = TrustedRenderDecision.from_record({
        "action": "answer",
        "evidence": [{"record_id": "r1", "text": "Authorized fact.", "status": "active"}],
    })
    legacy_output, _ = enforce_render_contract(legacy, None)
    assert legacy.legacy_plan and legacy_output["answer"] == "Authorized fact."


def test_component_routing_schema_and_locked_default():
    assert OnlineOptions().renderer_mode == "locked"
    memory = {"decision": "CODE_OR_SCHEMA_CHANGE", "failure_classification": "FACT_SELECTION",
              "repair_component": "memory", "diagnosis": "selection omitted a responsive fact",
              "specific_hypothesis": "changing selection changes required claims",
              "focused_goal": "select responsive claims", "work_order": ["change selection"],
              "repair_scope": {"subsystem": "selection", "file": "memory_system/agent.py",
                               "function": "query"},
              "safety_preservation_checks": ["preserve fixed runner gates"],
              "trace_citations": ["target:selection"],
              "addressed_decision_rule": "coverage_selected",
              "predicted_replay_change": {"observable": "required_claims", "expected_change": "include the responsive fact"},
              "intended_target_ids": ["target"]}
    OnlineRunner._validate_focused_design(memory, require_targets=True)
    with pytest.raises(ValidationFailure, match="owned by renderer"):
        OnlineRunner._validate_focused_design(
            {**memory, "failure_classification": "RENDERING"}, require_targets=True)


def test_backend_has_single_explicit_ownership_transfer():
    source = Path("online/backend.py").read_text(encoding="utf-8")
    assert "output.update(rendered)" not in source
    assert "final_prediction =" in source
    normalized = Backend._normalize_architect_design({
        "failure_classification": "RENDERING", "work_order": "change renderer"})
    assert normalized["repair_component"] == "renderer"
    assert normalized["work_order"] == ["change renderer"]


def test_renderer_artifact_is_declarative_and_contract_is_not_in_workspace():
    artifact = json.loads(Path("templates/renderer/renderer.json").read_text())
    assert artifact["schema_version"] == 1
    assert not Path("templates/renderer_contract.py").exists()
