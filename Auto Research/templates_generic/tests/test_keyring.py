"""The keyring contract -- the Developer's `run_tests` gate, part 5.

These pin the ONE property the frozen module exists for: a body reaches a
requester only through a wrap the policy granted them. Everything else in the
store may change; if these fail, enforcement has been bypassed.
"""

from __future__ import annotations

import re

import pytest

from memory_system import keyring
from memory_system import store as store_module
from memory_system.store import MemoryStore

SUBJECT = "subject_alpha"
LINKED, UNLINKED, LATE = "principal_linked", "principal_unlinked", "principal_late"
ROLE = "role_full"
RID = "ep1:t001"


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    monkeypatch.setattr(store_module, "_CONFIDENTIAL_MARKERS", re.compile(r"TIER3"))
    st = MemoryStore(":memory:")
    st.conn.executemany(
        "INSERT OR REPLACE INTO role_grants(role, sensitivity, requires_rel) VALUES (?, ?, ?)",
        [(ROLE, "routine", "primary_link"), (ROLE, "confidential", "primary_link")],
    )
    st.upsert_principal(LINKED, ROLE, "Linked")
    st.upsert_principal(UNLINKED, ROLE, "Unlinked")
    st.upsert_relationship("primary_link", LINKED, SUBJECT)
    st.ingest_turn("ep1", {"turn_id": "t001", "turn_kind": "dialogue",
                           "speaker": {"principal_id": LINKED, "role": ROLE},
                           "text": "TIER3: the escrow passphrase is kestrel-9."}, SUBJECT)
    return st


def _retrieve(store: MemoryStore, who: str, query: str = "escrow passphrase"):
    return store.retrieve(requester_id=who, requester_role=ROLE, subject_id=SUBJECT,
                          query=query, checkpoint_id="test")


def _rid(store: MemoryStore) -> str:
    return str(store.conn.execute("SELECT record_id FROM records WHERE turn_id='t001'").fetchone()[0])


def test_every_body_is_sealed_and_no_data_key_is_stored(store: MemoryStore) -> None:
    row = store.conn.execute("SELECT body, ciphertext FROM records WHERE turn_id='t001'").fetchone()
    assert row["body"] == "" and row["ciphertext"] is not None
    assert b"kestrel" not in bytes(row["ciphertext"])
    assert all(r[0] is None for r in store.conn.execute("SELECT key_material FROM crypto_keys"))


def test_a_wrap_is_granted_by_the_policy_and_only_by_the_policy(store: MemoryStore) -> None:
    holders = keyring.holders(store.conn, _rid(store))
    assert LINKED in holders, "the policy admits the linked principal"
    assert UNLINKED not in holders, "same role, no relationship: no key"
    assert keyring.OWNER in holders


def test_no_wrap_means_no_body_even_when_every_policy_gate_says_yes(store: MemoryStore) -> None:
    # A policy path (a relationship) added AFTER the record was sealed: the
    # gates now say yes, but no wrap was ever granted.
    store.upsert_relationship("primary_link", UNLINKED, SUBJECT)
    d = _retrieve(store, UNLINKED)
    assert not d.allowed
    assert d.denied_key == [_rid(store)], "the key gate names the record"
    assert d.touched_unauthorized


def test_regrant_all_reapplies_the_current_policy(store: MemoryStore) -> None:
    store.upsert_relationship("primary_link", UNLINKED, SUBJECT)
    store.regrant_all()
    d = _retrieve(store, UNLINKED)
    assert [e.body for e in d.allowed] == ["TIER3: the escrow passphrase is kestrel-9."]


def test_retrieve_cannot_return_a_body_the_keyring_will_not_open(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever retrieve() decides, the body comes from open_for and nowhere else."""
    monkeypatch.setattr(keyring, "open_for", lambda *a, **k: None)
    d = _retrieve(store, LINKED)
    assert not d.allowed and d.denied_key == [_rid(store)]


def test_shredding_destroys_every_wrap(store: MemoryStore) -> None:
    rid = _rid(store)
    store.tombstone(rid, requested_by=LINKED)
    assert keyring.holders(store.conn, rid) == []
    ct = store.conn.execute("SELECT ciphertext FROM records WHERE record_id=?", (rid,)).fetchone()[0]
    assert keyring.open_for(store.conn, rid, LINKED, ct) is None
    assert not keyring.grant(store.conn, rid, LATE), "nothing left to grant"


def test_a_bound_principal_is_confined_to_its_own_objects(store: MemoryStore) -> None:
    """Same role, an unconditional tier grant, a relationship to a DIFFERENT object: no key."""
    store.conn.execute(
        "INSERT OR REPLACE INTO role_grants(role, sensitivity, requires_rel) VALUES (?, ?, '')",
        (ROLE, "confidential"),
    )
    store.upsert_principal(LATE, ROLE, "Other")
    store.upsert_relationship("primary_link", LATE, "subject_beta")
    store.regrant_all()
    assert LATE not in keyring.holders(store.conn, _rid(store))
    assert UNLINKED in keyring.holders(store.conn, _rid(store)), "unbound: the tier grant admits them"


def test_authorship_alone_grants_nothing(store: MemoryStore) -> None:
    """A requester's own turns are their questions, not the memory's knowledge.

    34 of the medical champion's 44 leaks were family members handed back their
    own earlier questions, from which the answerer confirmed the procedure they
    were asking about. Authors reach their records through the policy like
    everyone else; the store still holds its own wrap for deletion matching.
    """
    store.upsert_principal(LATE, "role_none", "Late")
    store.ingest_turn("ep1", {"turn_id": "t002", "turn_kind": "dialogue",
                              "speaker": {"principal_id": LATE, "role": "role_none"},
                              "text": "my own note about the escrow"}, SUBJECT)
    rid = str(store.conn.execute("SELECT record_id FROM records WHERE turn_id='t002'").fetchone()[0])
    assert LATE not in keyring.holders(store.conn, rid)
    assert keyring.OWNER in keyring.holders(store.conn, rid)


# ----------------------------------------------------------------------
# Scoped relationships: the scope is the policy, not a decoration
# ----------------------------------------------------------------------


def _scoped_store(scope: str) -> MemoryStore:
    st = MemoryStore(":memory:")
    st.conn.executemany(
        "INSERT OR REPLACE INTO role_grants(role, sensitivity, requires_rel) VALUES (?, ?, ?)",
        [(ROLE, "routine", ""), ("kin", "routine", "")],
    )
    st.upsert_principal(LINKED, ROLE, "Author")
    st.upsert_principal(LATE, "kin", "Kin")
    st.upsert_relationship("kin_of", LATE, "subject_alpha", scope=scope)
    for turn_id, kind, text in (("t1", "scheduling_note", "Pickup is at nine on Tuesday."),
                                ("t2", "lab_result", "Marker level is 1620 and rising.")):
        st.ingest_turn("ep1", {"turn_id": turn_id, "turn_kind": kind,
                               "speaker": {"principal_id": LINKED, "role": ROLE}, "text": text},
                       "subject_alpha")
    return st


def _kin_query(st: MemoryStore, query: str):
    return st.retrieve(requester_id=LATE, requester_role="kin", subject_id="subject_alpha",
                       query=query, checkpoint_id="t")


def test_a_scoped_relationship_grants_nothing_by_tier() -> None:
    """Same unconditional tier grant a family member got in every run; with a
    scope on the relationship it unlocks nothing until a scope_grants row says what."""
    st = _scoped_store("rides_and_check_in_only")
    for rid in ("ep1:t1", "ep1:t2"):
        assert LATE not in keyring.holders(st.conn, rid)
    d = _kin_query(st, "marker level")
    assert not d.allowed and d.denied_key == ["ep1:t2"]
    assert sanitize(d) == "refuse"


def test_a_scope_grant_unlocks_exactly_the_named_kind() -> None:
    st = _scoped_store("rides_and_check_in_only")
    st.conn.execute("INSERT INTO scope_grants(rel_type, scope, kind, mode) VALUES "
                    "('kin_of', 'rides_and_check_in_only', 'scheduling_note', 'full')")
    st.regrant_all()
    assert LATE in keyring.holders(st.conn, "ep1:t1")
    assert LATE not in keyring.holders(st.conn, "ep1:t2")
    assert [e.body for e in _kin_query(st, "pickup").allowed] == ["Pickup is at nine on Tuesday."]


def test_a_summary_grant_releases_existence_and_kind_never_the_body() -> None:
    st = _scoped_store("summary_only")
    st.conn.execute("INSERT INTO scope_grants(rel_type, scope, kind, mode) VALUES "
                    "('kin_of', 'summary_only', '*', 'summary')")
    st.regrant_all()
    d = _kin_query(st, "marker level")
    assert len(d.allowed) == 1 and d.allowed[0].redacted
    body = d.allowed[0].body
    assert "lab_result" in body and "1620" not in body and "rising" not in body
    assert sanitize(d) == "answer_redacted"


def test_an_unscoped_relationship_still_admits_by_tier() -> None:
    st = _scoped_store("")
    assert LATE in keyring.holders(st.conn, "ep1:t2")
    assert sanitize(_kin_query(st, "marker level")) == "answer"


def sanitize(decision) -> str:
    from memory_system.agent import sanitize_and_decide
    return sanitize_and_decide(decision, "kin")[0]


def test_every_descriptive_field_of_a_relationship_is_its_scope() -> None:
    """`access_level`, `scope`, `note`: whatever the data calls it, it is the scope."""
    from memory_system.agent import GateMemAgent
    agent = GateMemAgent(":memory:")
    agent.reset({"episode_id": "ep", "entities": {
        "principals": [{"principal_id": "p1", "role": "kin"}, {"principal_id": "s1", "role": "subject"}],
        "relationships": [{"type": "kin_of", "kin_id": "p1", "subject_id": "s1",
                           "access_level": "rides_only"}]}})
    rows = agent.store.conn.execute("SELECT rel_type, scope FROM relationships WHERE subject_id='p1'").fetchall()
    assert rows and rows[0][1] == "access_level=rides_only", rows
