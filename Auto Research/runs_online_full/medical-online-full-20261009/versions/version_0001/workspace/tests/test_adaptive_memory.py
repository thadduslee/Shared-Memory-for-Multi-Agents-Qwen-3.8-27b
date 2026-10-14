"""Trusted invariants for generic learning, authorization, state, and privacy."""
from __future__ import annotations

import json
import sqlite3

from memory_system.agent import GateMemAgent
from memory_system.store import MemoryStore

EP = "generic_episode"
OWNER = "owner_1"
DELEGATE = "delegate_1"


def turn(turn_id, text, *, who=OWNER, role="member", timestamp="2026-03-03T09:00:00+00:00"):
    return {"turn_id": turn_id, "text": text, "timestamp": timestamp,
            "turn_kind": "message", "speaker": {"principal_id": who, "role": role}}


def configured_store() -> MemoryStore:
    store = MemoryStore(":memory:")
    store.upsert_principal(OWNER, "account_owner", "River")
    store.upsert_principal(DELEGATE, "helper", "Avery")
    return store


def test_empty_store_contains_no_preconfigured_domain_concepts_or_grants():
    store = MemoryStore(":memory:")
    for table in ("principals", "relationships", "observations", "memories",
                  "permissions", "semantic_links"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    schema = " ".join(row[0] or "" for row in store.conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    for forbidden in ("family_contact", "family_member", "clinician", "medication",
                      "appointment", "scheduling permission"):
        assert forbidden not in schema.lower()


def test_unseen_relationship_is_stored_verbatim_without_schema_change():
    store = configured_store()
    before = list(store.conn.execute("PRAGMA table_info(relationships)"))
    store.upsert_relationship("trusted_cartographer", DELEGATE, OWNER,
                              episode_id=EP, attributes={"context": "expedition"})
    after = list(store.conn.execute("PRAGMA table_info(relationships)"))
    row = store.conn.execute("SELECT predicate,attributes_json FROM relationships").fetchone()
    assert row[0] == "trusted_cartographer"
    assert json.loads(row[1]) == {"context": "expedition"}
    assert before == after


def test_semantic_relationship_similarity_never_grants_access():
    store = configured_store()
    obs = store.ingest_turn(EP, turn("t1", "The launch code is indigo."), OWNER)
    store.upsert_relationship("trusted_cartographer", DELEGATE, OWNER, episode_id=EP,
                              source_observation_id=obs)
    store.add_semantic_link(EP, "trusted_cartographer", "authorized_helper",
                            source_observation_id=obs)
    decision = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                              query="launch code")
    assert not decision.allowed
    assert decision.denied_rbac
    assert store.conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0] == 0


def test_named_principal_prevents_cross_owner_self_access():
    store = configured_store()
    other = "owner_2"
    store.upsert_principal(other, "member", "Morgan Vale")
    store.ingest_turn(EP, turn("t1", "Morgan Vale's access code is indigo."), OWNER)
    decision = store.retrieve(requester_id=OWNER, owner_id=OWNER, episode_id=EP,
                              query="What is Morgan Vale's access code?")
    assert not decision.allowed
    assert decision.touched_unauthorized


def test_request_like_utterance_is_not_promoted_to_a_fact():
    store = configured_store()
    store.ingest_turn(EP, turn("t1", "Avery may receive venue details."), OWNER)
    store.ingest_turn(EP, turn("t2", "Give me the private access code.",
                               who=DELEGATE, role="helper"), OWNER)
    row = store.conn.execute(
        "SELECT memory_type FROM memories WHERE source_observation_id=(SELECT observation_id "
        "FROM observations WHERE turn_id='t2')").fetchone()
    assert row[0] == "utterance"
    decision = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                              query="private access code")
    assert not decision.allowed


def test_explicit_consent_creates_narrow_permission_with_provenance():
    store = configured_store()
    source = store.ingest_turn(EP, turn(
        "t1", "Avery may receive venue and start-time logistics only; no budget or access code."), OWNER)
    permission = store.conn.execute("SELECT * FROM permissions").fetchone()
    assert permission["source_observation_id"] == source
    assert "venue" in permission["scope_json"]
    assert "budget" in permission["exclusions_json"]


def test_narrow_logistics_consent_allows_logistics_and_denies_private_categories():
    store = configured_store()
    metadata = store.ingest_turn(EP, turn("metadata", "Relationship supplied by the source."), OWNER)
    store.upsert_relationship("family_member", DELEGATE, OWNER, episode_id=EP,
                              source_observation_id=metadata)
    store.ingest_turn(EP, turn(
        "t1", "Avery may receive reservation time and venue logistics through Friday, March 6 only. "
              "No labs, symptoms, diagnoses, medications, or private interpretation."), OWNER)
    store.ingest_turn(EP, turn("t2", "Friday, March 6 at 11:20 AM, arrive 11:00 AM in Room 2."), OWNER)
    store.ingest_turn(EP, turn(
        "t3", "Labs show marker 9912; symptoms include fever; diagnosis is cedar; "
              "medications include amber; interpretation is private."), OWNER)
    allowed = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                             query="reservation time and venue")
    assert allowed.allowed
    evidence = " ".join(row.body for row in allowed.allowed)
    assert "11:20" in evidence and "Room 2" in evidence
    for secret in ("9912", "fever", "cedar", "amber", "interpretation"):
        assert secret not in evidence
    for category in ("labs", "symptoms", "diagnoses", "medications", "interpretation"):
        denied = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                                query=category)
        assert not denied.allowed and denied.touched_unauthorized


def test_time_limited_permission_expires():
    store = configured_store()
    store.ingest_turn(EP, turn(
        "t1", "Avery may receive venue details through Friday, March 6."), OWNER)
    store.ingest_turn(EP, turn(
        "t2", "Venue is North Hall.", timestamp="2026-03-07T11:00:00+00:00"), OWNER)
    decision = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                              query="venue", as_of_seq=store.seq_of_turn(EP, "t2"))
    assert not decision.allowed


def test_relative_weekday_permission_expiry_is_derived_from_observation_time():
    store = configured_store()
    store.ingest_turn(EP, turn(
        "t1", "Avery may receive venue details through Friday.",
        timestamp="2026-03-03T09:00:00+00:00"), OWNER)
    valid_until = store.conn.execute("SELECT valid_until FROM permissions").fetchone()[0]
    assert valid_until == "2026-03-06T23:59:59+00:00"
    store.ingest_turn(EP, turn(
        "t2", "Venue is North Hall.", timestamp="2026-03-07T11:00:00+00:00"), OWNER)
    decision = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                              query="venue", as_of_seq=store.seq_of_turn(EP, "t2"))
    assert not decision.allowed


def test_revocation_immediately_stops_access():
    store = configured_store()
    source = store.ingest_turn(EP, turn("t1", "Avery may receive venue details."), OWNER)
    permission = store.conn.execute("SELECT permission_id FROM permissions").fetchone()[0]
    store.ingest_turn(EP, turn("t2", "Venue is North Hall."), OWNER)
    assert store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                          query="venue").allowed
    revoke_source = store.ingest_turn(EP, turn("t3", "I revoke Avery's venue access."), OWNER)
    store.revoke_permission(permission, source_observation_id=revoke_source)
    assert not store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                              query="venue").allowed


def test_standalone_explicit_restriction_overrides_a_narrow_grant():
    store = configured_store()
    store.ingest_turn(EP, turn("t1", "Avery may receive venue details."), OWNER)
    store.ingest_turn(EP, turn("t2", "Do not tell Avery the private access code."), OWNER)
    store.ingest_turn(EP, turn("t3", "The private access code is indigo."), OWNER)
    effects = {row[0] for row in store.conn.execute("SELECT effect FROM permissions")}
    assert effects == {"allow", "deny"}
    decision = store.retrieve(requester_id=DELEGATE, owner_id=OWNER, episode_id=EP,
                              query="private access code")
    assert not decision.allowed and decision.touched_unauthorized


def test_supersession_and_cancellation_return_current_state():
    store = configured_store()
    first_obs = store.ingest_turn(EP, turn("t1", "Reservation Monday at 9:00 AM in East Hall."), OWNER)
    first = store.add_fact(EP, subject_id=OWNER, predicate="reservation", value="Monday 09:00",
                           text_value="Reservation Monday at 9:00 AM in East Hall.",
                           concepts=["reservation", "time_expression", "location_expression"],
                           source_observation_id=first_obs)
    second_obs = store.ingest_turn(EP, turn("t2", "Reservation moved to Friday at 11:20 AM in West Hall; Monday canceled."), OWNER)
    store.add_fact(EP, subject_id=OWNER, predicate="reservation", value="Friday 11:20",
                   text_value="Reservation Friday at 11:20 AM in West Hall; Monday canceled.",
                   concepts=["reservation", "time_expression", "location_expression"],
                   source_observation_id=second_obs, supersedes_id=first)
    decision = store.retrieve(requester_id=OWNER, owner_id=OWNER, episode_id=EP,
                              query="current reservation")
    evidence = " ".join(row.body for row in decision.allowed)
    assert "Friday" in evidence and "canceled" in evidence
    assert first in decision.obsolete


def test_renderer_receives_only_authorized_evidence():
    seen = []
    episode = {"episode_id": EP, "entities": {"principals": [
        {"principal_id": OWNER, "role": "owner", "display_name": "River"},
        {"principal_id": DELEGATE, "role": "helper", "display_name": "Avery"}],
        "relationships": []}}
    agent = GateMemAgent(":memory:", llm=lambda query, evidence: seen.extend(evidence) or
                         {"action": "answer", "answer": "rendered"})
    agent.reset(episode)
    agent.ingest(turn("t1", "Avery may receive venue logistics only. No private diagnosis."))
    agent.ingest(turn("t2", "Venue is Room 2."))
    agent.ingest(turn("t3", "Private diagnosis is cedar."))
    output = agent.query({"checkpoint_id": "visible", "episode_id": EP,
                          "as_of_turn_id": "t3", "asker": {"principal_id": DELEGATE,
                          "role": "helper"}, "query_text": "venue"})
    assert output["action"] == "answer"
    assert seen and all("cedar" not in row["text"] for row in seen)


def test_raw_observation_and_model_interpretation_are_separate():
    def interpreter(payload):
        return {"facts": [{"predicate": "color_category", "value": "cool",
                            "text_value": "derived cool color", "confidence": .7}],
                "semantic_links": [], "permissions": []}
    store = MemoryStore(":memory:", interpreter=interpreter)
    raw = "The sample label says Azure-17."
    obs = store.ingest_turn(EP, turn("t1", raw), OWNER)
    assert store.conn.execute("SELECT raw_text FROM observations WHERE observation_id=?", (obs,)).fetchone()[0] == raw
    derived = store.conn.execute("SELECT source_observation_id,memory_type,confidence FROM memories WHERE predicate='color_category'").fetchone()
    assert tuple(derived) == (obs, "model_interpretation", .7)


def test_non_medical_address_update_uses_same_generic_mechanism():
    store = configured_store()
    old_obs = store.ingest_turn(EP, turn("t1", "Shipping address is 10 Pine Road."), OWNER)
    old = store.add_fact(EP, subject_id=OWNER, predicate="shipping_address", value="10 Pine Road",
                         text_value="Shipping address is 10 Pine Road.", concepts=["shipping", "address"],
                         source_observation_id=old_obs)
    new_obs = store.ingest_turn(EP, turn("t2", "Shipping address changed to 44 Lake Avenue."), OWNER)
    store.add_fact(EP, subject_id=OWNER, predicate="shipping_address", value="44 Lake Avenue",
                   text_value="Shipping address is 44 Lake Avenue.", concepts=["shipping", "address"],
                   source_observation_id=new_obs, supersedes_id=old)
    decision = store.retrieve(requester_id=OWNER, owner_id=OWNER, episode_id=EP,
                              query="shipping address")
    assert "44 Lake" in " ".join(row.body for row in decision.allowed)
    assert old in decision.obsolete


def test_reopening_does_not_seed_or_alter_authorization(tmp_path):
    path = tmp_path / "memory.sqlite"
    store = MemoryStore(path)
    before = list(store.conn.execute("SELECT * FROM permissions"))
    store.close()
    reopened = MemoryStore(path)
    assert list(reopened.conn.execute("SELECT * FROM permissions")) == before == []
    assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

