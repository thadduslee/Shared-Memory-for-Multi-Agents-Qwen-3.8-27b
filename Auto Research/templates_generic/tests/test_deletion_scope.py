"""Regression tests for the two ways the deletion path has broken a run.

Both failures were invisible to the existing suite: a build that tombstoned the
whole episode on any delete request, and a `tombstone()` that raised
IntegrityError the second time it saw a record.  Each cost a full iteration.
"""

from __future__ import annotations

import pytest

from memory_system.store import MemoryStore

PATIENT = "patient_ana"
CLINICIAN = "clinician_okafor"
SECRET = "safe callback number 555-0142"
KEEP = "metformin 500 mg twice daily with food"


# THE POLICY UNDER TEST IS INSTALLED HERE, not inherited.
# `DEFAULT_ROLE_GRANTS` is empty in this template -- the access policy is the
# domain's to author, and a seed that shipped one would hand every other domain
# a wrong one. These tests therefore carry their own grants, so they assert the
# store's MECHANISM rather than any domain's rules, and they keep passing after
# a domain authors grants of its own. The role names below are just fixture
# labels; nothing outside this file depends on them.
FIXTURE_GRANTS: tuple[tuple[str, str, str], ...] = (
    ("patient", "routine", "self"),
    ("clinician", "routine", "fixture_primary_link"),
    ("clinician", "restricted", "fixture_primary_link"),
    ("clinician", "confidential", "fixture_primary_link"),
)


@pytest.fixture()
def store() -> MemoryStore:
    st = MemoryStore(":memory:")
    st.conn.executemany(
        "INSERT OR REPLACE INTO role_grants(role, sensitivity, requires_rel) VALUES (?, ?, ?)",
        FIXTURE_GRANTS,
    )
    st.upsert_principal(PATIENT, "patient")
    st.upsert_principal(CLINICIAN, "clinician")
    st.upsert_relationship("fixture_primary_link", CLINICIAN, PATIENT)
    st.ingest_turn("ep1", {"turn_id": "t001", "turn_kind": "portal_message",
                           "speaker": {"principal_id": PATIENT, "role": "patient"},
                           "text": f"Please store this: {SECRET}."}, PATIENT)
    st.ingest_turn("ep1", {"turn_id": "t002", "turn_kind": "portal_message",
                           "speaker": {"principal_id": CLINICIAN, "role": "clinician"},
                           "text": f"Plan: {KEEP}."}, PATIENT)
    return st


def test_deletion_request_spares_unrelated_records(store: MemoryStore) -> None:
    """A delete request must tombstone what it names, not the whole episode."""
    store.ingest_turn("ep1", {"turn_id": "t003", "turn_kind": "portal_message",
                              "speaker": {"principal_id": PATIENT, "role": "patient"},
                              "text": f"Actually, delete {SECRET} from the shared assistant memory."},
                      PATIENT)
    assert store.is_deleted("ep1:t001"), "the deletion request did not match its target"
    assert not store.is_deleted("ep1:t002"), (
        "the deletion request tombstoned an unrelated record; a whole-episode "
        "wipe drives U to zero because nothing survives to retrieve"
    )
    decision = store.retrieve(requester_id=CLINICIAN, requester_role="clinician",
                              subject_id=PATIENT, query="metformin dose")
    assert decision.allowed, "no record survived the deletion request"


def test_repeated_deletion_requests_do_not_raise(store: MemoryStore) -> None:
    """Two delete requests in one episode must not crash the shard."""
    for turn_id in ("t003", "t004"):
        store.ingest_turn("ep1", {"turn_id": turn_id, "turn_kind": "portal_message",
                                  "speaker": {"principal_id": PATIENT, "role": "patient"},
                                  "text": f"Please delete {SECRET} from memory."},
                          PATIENT)
    assert store.is_deleted("ep1:t001")


def test_tombstone_is_idempotent(store: MemoryStore) -> None:
    """`tombstone()` is called from several paths; the second must be a no-op."""
    store.tombstone("ep1:t001", requested_by=PATIENT, reason="first")
    store.tombstone("ep1:t001", requested_by=PATIENT, reason="second")
    assert store.is_deleted("ep1:t001")
