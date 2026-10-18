from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from online.failure_feedback import (
    categorize, sanitize_build_feedback, sanitized_failure_feedback,
)
from online.runner import OnlineOptions, OnlineRunner
from renderer_contract import TrustedRenderDecision, enforce_render_contract
from templates.memory_system.policy import (
    AuthorizationRequest,
    PolicyEvaluator,
    Principal,
    Resource,
)


def test_failure_feedback_is_allowlisted_and_content_free():
    secret = "PRIVATE-REFERENCE-7f31"
    report = {
        "safe_parent_version": "version_0001",
        "candidate_system_version": "version_0002",
        "phase": "rejected_with_feedback",
        "trigger_type": "scored_checkpoint_failure",
        "rejection_reason": "wrong_answer_no_targeted_improvement",
        "reasons": [f"expected {secret}"],
        "build_test_errors": [{"error": secret}],
        "targeted_replay": {
            "status": "no_improvement",
            "failed_checkpoint_ids": ["target-a"],
            "pairs": {"target-a": {
                "candidate": {"authorized_evidence_count": 0,
                              "vote_summary": {"outcome": "unanimous_fail", "valid": True}},
                "parent": {"vote_summary": {"outcome": "unanimous_fail", "valid": True}},
            }},
        },
    }
    feedback = sanitized_failure_feedback({"incident_id": "incident_1"}, report)
    encoded = json.dumps(feedback)
    assert secret not in encoded
    assert feedback["failure_category"] == "insufficient_or_missing_evidence"
    assert feedback["record_counts"]["selected"] == 0
    assert feedback["remaining_targets"] == ["target-a"]


def _failed_report(candidate):
    return {
        "trigger_type": "scored_checkpoint_failure",
        "rejection_reason": "wrong_answer_no_targeted_improvement",
        "targeted_replay": {
            "failed_checkpoint_ids": ["target"],
            "pairs": {"target": {
                "parent": {"vote_summary": {"outcome": "unanimous_fail", "valid": True}},
                "candidate": {**candidate, "passed": False,
                              "vote_summary": {"outcome": "unanimous_fail", "valid": True}},
            }},
        },
    }


def test_stage_aware_failure_classification_prefers_renderer_and_fact_selection():
    action_conflict = _failed_report({
        "authorized_evidence_count": 1, "required_claim_count": 1,
        "answer_plan_valid": True,
        "renderer_contract": {"conflict": True, "reasons": ["action_conflict"]},
    })
    malformed = _failed_report({
        "authorized_evidence_count": 1, "required_claim_count": 1,
        "answer_plan_valid": True,
        "renderer_contract": {"conflict": True, "reasons": ["malformed_renderer_output"]},
    })
    excessive = _failed_report({
        "authorized_evidence_count": 2, "required_claim_count": 2,
        "answer_plan_valid": True, "required_without_query_facets_count": 1,
        "renderer_contract": {"conflict": False, "reasons": []},
    })
    missing_required = _failed_report({
        "authorized_evidence_count": 1, "required_claim_count": 0,
        "answer_plan_valid": True,
        "renderer_contract": {
            "conflict": True,
            "reasons": ["malformed_renderer_output", "empty_authorized_answer"],
        },
    })
    assert categorize(action_conflict) == "correct_evidence_but_wrong_action"
    assert categorize(malformed) == "renderer_or_contract_failure"
    assert categorize(excessive) == "fact_selection_or_answer_plan_failure"
    assert categorize(missing_required) == "fact_selection_or_answer_plan_failure"
    feedback = sanitized_failure_feedback({"incident_id": "i"}, malformed)
    assert feedback["failure_stage"] == "RENDERING"
    assert feedback["suspected_subsystems"]["rendering"] is True
    assert feedback["suspected_subsystems"]["retrieval"] is False


def test_contract_and_fallback_differentials_are_reported_as_partial_progress():
    report = _failed_report({
        "authorized_evidence_count": 1, "required_claim_count": 1,
        "answer_plan_valid": True, "answer_source": "validated_renderer",
        "renderer_contract": {"conflict": False, "reasons": []},
    })
    report["targeted_replay"].update({
        "contract_result_changed": True, "fallback_use_changed": True,
        "answer_source_changed": True, "contract_reasons_before": ["unknown_claim"],
        "contract_reasons_after": [], "intermediate_contract_problem_disappeared": True,
        "final_result_changed": False,
    })
    pair = report["targeted_replay"]["pairs"]["target"]
    pair.update({"contract_result_changed": True, "fallback_use_changed": True,
                 "answer_source_changed": True})
    feedback = sanitized_failure_feedback({"incident_id": "i"}, report)
    assert feedback["behavior_changed_from_parent"] is True
    assert feedback["targeted_replay"]["intermediate_contract_problem_disappeared"] is True
    assert feedback["targeted_replay"]["final_result_changed"] is False


def _design(**updates):
    value = {
        "failure_classification": "RENDERING", "repair_component": "renderer",
        "diagnosis": "the contract rejected structurally invalid sentence attribution",
        "specific_hypothesis": "validating sentence claim ids before release changes the contract result",
        "focused_goal": "produce valid attributed sentences",
        "work_order": ["Validate sentence claim ids in the renderer artifact."],
        "repair_scope": {"subsystem": "renderer", "file": "renderer/renderer.json",
                         "function": "instruction_suffix"},
        "trace_citations": ["cp:renderer_contract:unknown_claim"],
        "addressed_decision_rule": "unknown_claim",
        "predicted_replay_change": {"observable": "contract_result",
                                    "expected_change": "unknown_claim disappears"},
        "change_type": "prompt_edit", "failure_mechanism": "claim id validation",
        "safety_preservation_checks": ["preserve privacy and deletion behavior"],
        "intended_target_ids": ["cp"], "decision": "CODE_OR_SCHEMA_CHANGE",
    }
    value.update(updates)
    return value


def test_normalized_repair_intent_rejects_rewording_but_allows_new_mechanism():
    first = OnlineRunner._normalized_repair_intent(_design())
    reworded = OnlineRunner._normalized_repair_intent(_design(
        diagnosis="sentence references were malformed",
        focused_goal="emit structurally sound sentence references",
        work_order=["Move the claim-ID reminder to the top of the prompt."],
        specific_hypothesis="repositioning the same validation wording fixes output"))
    different = OnlineRunner._normalized_repair_intent(_design(
        change_type="schema_change", failure_mechanism="parse schema",
        repair_scope={"subsystem": "renderer", "file": "renderer/renderer.json",
                      "function": "response_schema"}))
    assert first == reworded
    assert first != different


def test_diagnosis_review_uses_causal_stage_not_lexical_overlap():
    runner = object.__new__(OnlineRunner)
    incident = {
        "trigger_type": "scored_checkpoint_failure",
        "unresolved_target_checkpoint_ids": ["cp"],
        "architect_incident_dossier": {"targets": [{
            "checkpoint_id": "cp", "decision_trace": [{
                "trace_id": "cp:renderer_contract:unknown_claim",
                "decision_stage": "renderer_contract", "decision_rule": "unknown_claim",
            }, {
                "trace_id": "cp:record:r1", "decision_stage": "authorization_gate",
                "decision_rule": "outside_permission_scope",
            }],
        }]},
    }
    design = _design(diagnosis="a fluent paraphrase of the causal mechanism")
    intent = OnlineRunner._normalized_repair_intent(design)
    report = {"attempt_index": 1, "repair_intent_signature": __import__(
        "online.storage", fromlist=["digest"]).digest(intent)}
    accepted = runner._diagnosis_review(incident, design, report, [])
    assert accepted["status"] == "accepted"
    unrelated = _design(
        trace_citations=["cp:record:r1"],
        addressed_decision_rule="outside_permission_scope")
    unrelated_report = {"attempt_index": 2, "repair_intent_signature": "different"}
    rejected = runner._diagnosis_review(incident, unrelated, unrelated_report, [])
    assert rejected["status"] == "rejected"
    assert rejected["checks"]["cited_event_belongs_to_repair_stage"] is False


def test_developer_build_feedback_contains_only_signatures():
    secret = "DELETED-CONTENT-DO-NOT-REPEAT"
    feedback = sanitize_build_feedback({
        "stage": "unit_tests", "error": secret,
        "gate_reports": {"unit_tests": {"ok": False, "output": secret}},
    })
    assert secret not in json.dumps(feedback)
    assert feedback["gates"]["unit_tests"]["passed"] is False


def _decision(action="answer"):
    return TrustedRenderDecision.from_record({
        "action": action,
        "answer": "authorized body",
        "evidence": [{"record_id": "allowed", "text": "authorized body", "status": "active"},
                     {"record_id": "deleted", "text": "secret", "status": "deleted"}],
    })


def test_renderer_cannot_reverse_authorized_answer_to_refusal():
    output, conflict = enforce_render_contract(
        _decision(), {"action": "refuse", "answer": "", "used_record_ids": []})
    assert conflict["conflict"]
    assert output == {"action": "answer", "answer": "authorized body",
                      "used_record_ids": ["allowed"],
                      "renderer_fallback": True}


def test_renderer_cannot_answer_a_denied_decision():
    output, conflict = enforce_render_contract(
        _decision("refuse"),
        {"action": "answer", "answer": "secret", "used_record_ids": ["allowed"]})
    assert conflict["conflict"]
    assert output["action"] == "refuse" and output["answer"] == ""


def test_renderer_cannot_reference_unauthorized_or_deleted_records():
    output, conflict = enforce_render_contract(
        _decision(),
        {"action": "answer", "answer": "invented", "used_record_ids": ["deleted", "foreign"]})
    assert conflict["conflict"]
    assert output["used_record_ids"] == ["allowed"]
    assert output["answer"] == "authorized body"


def _request(*, role="viewer", resource_type="ticket", status="active",
             scope=None, context=None, relationships=(), denials=(), grants=()):
    return AuthorizationRequest(
        Principal("principal-1", frozenset({role})), "retrieve",
        Resource("resource-1", resource_type, "owner-1",
                 scope or {"tenant_id": "tenant-a"}, status),
        context or {"tenant_id": "tenant-a"}, tuple(relationships),
        tuple(grants), tuple(denials))


def test_policy_evaluator_is_generic_and_policy_changes_access():
    project_policy = {"isolation_boundaries": ["tenant_id"], "rules": [{
        "id": "project_reader", "effect": "allow", "actions": ["retrieve"],
        "resource_types": ["ticket"], "roles": ["project_reader"],
    }]}
    assert PolicyEvaluator(project_policy).evaluate(
        _request(role="project_reader")).allowed
    assert not PolicyEvaluator({**project_policy, "rules": []}).evaluate(
        _request(role="project_reader")).allowed
    assert not PolicyEvaluator(project_policy).evaluate(_request(role="unknown")).allowed


def test_policy_denial_revocation_invalidity_and_isolation_override_allows():
    policy = {"isolation_boundaries": ["tenant_id"], "rules": [{
        "id": "all_readers", "effect": "allow", "actions": ["retrieve"],
        "resource_types": ["ticket"], "roles": ["viewer"],
    }]}
    evaluator = PolicyEvaluator(policy)
    assert not evaluator.evaluate(_request(
        denials=({"principal_id": "principal-1", "action": "retrieve",
                  "resource_id": "resource-1"},))).allowed
    revoked = _request()
    revoked = replace(revoked, revocations=("resource-1",))
    assert not evaluator.evaluate(revoked).allowed
    assert not evaluator.evaluate(_request(status="deleted")).allowed
    assert not evaluator.evaluate(_request(
        scope={"tenant_id": "tenant-b"}, context={"tenant_id": "tenant-a"})).allowed


def test_explicit_grants_are_limited_to_declared_action_resource_and_scope():
    grant = ({"principal_id": "principal-1", "action": "retrieve",
              "resource_id": "resource-1", "resource_type": "ticket",
              "scope": {"tenant_id": "tenant-a"}},)
    policy = {"isolation_boundaries": ["tenant_id"]}
    evaluator = PolicyEvaluator(policy)
    assert evaluator.evaluate(_request(grants=grant)).allowed
    assert not evaluator.evaluate(replace(
        _request(grants=grant), action="update")).allowed
    assert not evaluator.evaluate(replace(
        _request(grants=grant),
        resource=Resource("resource-2", "ticket", "owner-1",
                          {"tenant_id": "tenant-a"}))).allowed
    assert not evaluator.evaluate(_request(
        scope={"tenant_id": "tenant-b"}, context={"tenant_id": "tenant-b"},
        grants=grant)).allowed


def test_medical_behavior_lives_in_policy_fixture_not_evaluator_source():
    path = Path("templates/memory_system/policies/medical.json")
    policy = json.loads(path.read_text())
    source = Path("templates/memory_system/policy.py").read_text().lower()
    assert "clinician" not in source and "appointment" not in source
    request = _request(
        role="clinician", resource_type="health_record",
        scope={"episode_id": "ep"}, context={"episode_id": "ep"},
        relationships=({"predicate": "assigned_clinician", "subject_id": "principal-1",
                        "object_id": "owner-1", "status": "active"},),
    )
    assert PolicyEvaluator(policy).evaluate(request).allowed


@pytest.mark.parametrize("votes", [0, -1, 2, 4])
def test_replay_vote_count_must_be_positive_and_odd(votes):
    with pytest.raises(ValueError, match="positive odd"):
        OnlineOptions(replay_votes=votes)


@pytest.mark.parametrize(("votes", "errors", "outcome", "passed"), [
    ([True, True, True], [], "unanimous_pass", True),
    ([True, True, False], [], "majority_pass", True),
    ([False, False, False], [], "unanimous_fail", False),
    ([False, False, True], [], "majority_fail", False),
    ([True, True], ["ValidationFailure"], "inconclusive_or_error", False),
])
def test_vote_aggregation_distinguishes_outcomes(votes, errors, outcome, passed):
    result = OnlineRunner._aggregate_votes(
        [({}, {"passed": vote}) for vote in votes], errors)
    assert result["outcome"] == outcome
    assert result["passed"] is passed
    assert result["valid"] is (not errors)
