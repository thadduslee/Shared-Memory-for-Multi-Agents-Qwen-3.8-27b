"""RBAC unit tests -- the Developer's `run_tests` gate, part 1.

These are the *local* tests: fast, deterministic, no LLM, no benchmark data.
They exist so the Developer's ReAct loop can find an access-control regression
in a second rather than after a full benchmark run.

WHY EVERY NAME HERE IS MADE UP
------------------------------
An earlier version of this file was written against a clinic: it asserted that
an `fixture_primary_link` could read a confidential record, that a `nurse` could
not, and that a `family_member` was capped at routine.  Those are facts about
ONE domain's policy, not about this store, and `tests_ok` is a hard gate -- so
pointing the loop at any other domain created a contradiction.  To score, it had
to author that domain's grants; to pass, it had to keep the clinic's.  Office
runs resolved this by carrying the clinical grants as dead rows and never
writing a coherent policy of their own, and two runs wedged outright with the
Developer burning all five retries against `test_fixture_primary_link_sees_
confidential`.

So the roles below are `role_full`, `role_open`, `role_scoped`; the relationship
is `primary_link`; the tiers are marked by `TIER2` and `TIER3`.  None of them
mean anything outside this file.  THE FIXTURE INSTALLS ITS OWN POLICY, rather
than leaning on `DEFAULT_ROLE_GRANTS` -- which is empty in this template, and is
the domain's to author.  What is asserted is the MECHANISM every domain needs:

    a grant whose relationship is unmet denies;  a grant whose relationship is
    met allows;  a tier with no grant row at all denies structurally.

A domain that wants clinicians and patients expresses that in its own grants and
its own episodes.  This file must stay true whichever domain that is.
"""

from __future__ import annotations

import re

import pytest

from memory_system import store as store_module
from memory_system.agent import GateMemAgent, sanitize_and_decide
from memory_system.store import MemoryStore

SUBJECT = "subject_alpha"
LINKED = "principal_linked"
UNLINKED = "principal_unlinked"
OPEN = "principal_open"
SCOPED = "principal_scoped"

ROLE_FULL = "role_full"      # every tier, but only over a `primary_link`
ROLE_OPEN = "role_open"      # routine only, no relationship required
ROLE_SCOPED = "role_scoped"  # routine only, and only over a `limited_link`

# The policy this file asserts against. Written out here so the test is
# self-contained: it passes on an empty DEFAULT_ROLE_GRANTS and keeps passing
# whatever grants a domain later adds.
FIXTURE_GRANTS = (
    (ROLE_FULL, "routine", "primary_link"),
    (ROLE_FULL, "restricted", "primary_link"),
    (ROLE_FULL, "confidential", "primary_link"),
    (ROLE_OPEN, "routine", ""),
    (ROLE_SCOPED, "routine", "limited_link"),
)

ROUTINE_TEXT = "Rotate the inventory tag to 4120."
TIER3_TEXT = "TIER3: the escrow passphrase is kestrel-9."
TIER3_TERM = "escrow passphrase"
TIER3_SECRET = "kestrel-9"


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    # The template's classifiers match nothing on purpose -- sensitivity is a
    # domain vocabulary. The fixture supplies a throwaway one so the tiers under
    # test actually exist, without asserting anything about which words a real
    # domain would use.
    monkeypatch.setattr(store_module, "_CONFIDENTIAL_MARKERS", re.compile(r"TIER3"))
    monkeypatch.setattr(store_module, "_RESTRICTED_MARKERS", re.compile(r"TIER2"))

    st = MemoryStore(":memory:")
    st.conn.executemany(
        "INSERT OR REPLACE INTO role_grants(role, sensitivity, requires_rel) VALUES (?, ?, ?)",
        FIXTURE_GRANTS,
    )
    st.upsert_principal(LINKED, ROLE_FULL, "Linked")
    st.upsert_principal(UNLINKED, ROLE_FULL, "Unlinked")
    st.upsert_principal(OPEN, ROLE_OPEN, "Open")
    st.upsert_principal(SCOPED, ROLE_SCOPED, "Scoped")
    st.upsert_relationship("primary_link", LINKED, SUBJECT)
    st.upsert_relationship("limited_link", SCOPED, SUBJECT, "narrow")

    st.ingest_turn("ep1", {"turn_id": "t001", "turn_kind": "dialogue",
                           "speaker": {"principal_id": LINKED, "role": ROLE_FULL},
                           "text": ROUTINE_TEXT}, SUBJECT)
    st.ingest_turn("ep1", {"turn_id": "t002", "turn_kind": "note_update",
                           "speaker": {"principal_id": LINKED, "role": ROLE_FULL},
                           "text": TIER3_TEXT}, SUBJECT)
    return st


def _retrieve(store: MemoryStore, who: str, role: str, query: str):
    return store.retrieve(requester_id=who, requester_role=role,
                          subject_id=SUBJECT, query=query, checkpoint_id="test")


def test_confidential_is_encrypted_at_rest(store: MemoryStore) -> None:
    """No body sits in plaintext in the `body` column -- confidential or not."""
    row = store.conn.execute(
        "SELECT body, ciphertext, key_id, sensitivity FROM records WHERE turn_id = 't002'"
    ).fetchone()
    assert row["sensitivity"] == "confidential"
    assert row["body"] == ""
    assert row["ciphertext"] is not None
    assert row["key_id"] is not None


def test_a_met_relationship_grant_allows(store: MemoryStore) -> None:
    """`role_full` holds `primary_link` over the subject, so every tier clears."""
    decision = _retrieve(store, LINKED, ROLE_FULL, TIER3_TERM)
    bodies = " ".join(e.body for e in decision.allowed)
    assert TIER3_SECRET in bodies
    assert not decision.denied_scope


def test_an_unmet_relationship_grant_denies(store: MemoryStore) -> None:
    """Same role, same grant, no relationship: the grant alone is not enough."""
    decision = _retrieve(store, UNLINKED, ROLE_FULL, TIER3_TERM)
    assert not decision.allowed
    assert decision.touched_unauthorized


def test_a_tier_with_no_grant_row_is_denied_structurally(store: MemoryStore) -> None:
    """`role_open` has a routine row and nothing above it, so the deny is RBAC."""
    decision = _retrieve(store, OPEN, ROLE_OPEN, TIER3_TERM)
    assert not any(e.sensitivity == "confidential" for e in decision.allowed)
    assert decision.denied_rbac


def test_a_scoped_role_is_capped_at_its_granted_tier(store: MemoryStore) -> None:
    decision = _retrieve(store, SCOPED, ROLE_SCOPED, TIER3_TERM)
    assert not decision.allowed
    assert decision.touched_unauthorized


def test_a_scoped_role_still_gets_its_granted_tier(store: MemoryStore) -> None:
    decision = _retrieve(store, SCOPED, ROLE_SCOPED, "inventory tag")
    assert all(e.sensitivity == "routine" for e in decision.allowed)


def test_decision_maps_to_refuse_not_answer() -> None:
    from memory_system.store import Decision

    decision = Decision(denied_rbac=["r1"])
    action, _ = sanitize_and_decide(decision, ROLE_OPEN)
    assert action == "refuse"


def test_partial_authorization_is_redacted() -> None:
    from memory_system.store import Decision, Evidence

    decision = Decision(
        allowed=[Evidence("r1", "t1", ROLE_FULL, "dialogue", "routine", ROUTINE_TEXT)],
        denied_rbac=["r2"],
    )
    action, _ = sanitize_and_decide(decision, ROLE_OPEN)
    assert action == "answer_redacted"


def test_every_denial_is_audited(store: MemoryStore) -> None:
    _retrieve(store, SCOPED, ROLE_SCOPED, TIER3_TERM)
    rows = store.conn.execute(
        "SELECT decision FROM access_log WHERE requester_id = ?", (SCOPED,)
    ).fetchall()
    assert rows, "denials must be written to access_log for Critic evidence"
    assert all(r["decision"] in {"allow", "deny_rbac", "deny_scope", "deny_tombstone"} for r in rows)


def test_agent_never_reads_hidden_annotations() -> None:
    """The field wall, tested at the agent boundary."""
    agent = GateMemAgent(":memory:")
    agent.reset({
        "episode_id": "ep1",
        "entities": {"principals": [{"principal_id": OPEN, "role": ROLE_OPEN}],
                     "relationships": []},
    })
    agent.ingest({"turn_id": "t001", "turn_kind": "dialogue",
                  "speaker": {"principal_id": OPEN, "role": ROLE_OPEN},
                  "text": ROUTINE_TEXT})
    out = agent.query({
        "checkpoint_id": "c1", "episode_id": "ep1", "as_of_turn_id": "t001",
        "asker": {"principal_id": OPEN, "role": ROLE_OPEN},
        "query_text": "what is the inventory tag",
        # Poison: if any of these influence the output, the wall has a hole.
        "expected_action": "refuse",
        "judge_spec": {"include": ["SHOULD-NOT-APPEAR"]},
        "leak_targets": ["SHOULD-NOT-APPEAR"],
        "query_type": "utility",
    })
    assert "SHOULD-NOT-APPEAR" not in str(out)
    assert out["action"] in {"answer", "answer_redacted", "refuse", "no_memory"}
