"""The contract -- the Developer's `run_tests` gate.

Property tests through the public interface on synthetic episodes. Every name
is meaningless on purpose (`kind_a`, `bond_x`, `obj_1`): the tests pin what
must hold for ANY design on ANY vocabulary, never what a dataset looks like.
The statements below are plain. The harness additionally runs held-out
adaptivity checks you cannot read -- paraphrased, unseen vocabularies, the
memory model enabled -- and reports which families failed (see TASK.md).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from memory_system import keyring
from memory_system.agent import ACTIONS, GateMemAgent


def make_episode(v: dict[str, str]) -> dict[str, Any]:
    """An episode over a vocabulary `v` (roles, relationship type, object name).

    Three declared principals. P1 and P2 hold `rel` over `obj`; P2's is scoped.
    P3 holds nothing. Turns: a schedule fact, a secret token, a consent grant
    to P3 for the schedule, a revocation of P2, a deletion of the token, a
    superseding schedule fact.
    """
    p1, p2, p3, obj = v["p1"], v["p2"], v["p3"], v["obj"]
    return {
        "episode_id": v["ep"],
        "entities": {
            "principals": [
                {"principal_id": p1, "role": v["r1"], "display_name": "One"},
                {"principal_id": p2, "role": v["r2"], "display_name": "Two"},
                {"principal_id": p3, "role": v["r3"], "display_name": "Three"},
            ],
            "relationships": [
                {"type": v["rel"], "holder_id": p1, "target_id": obj},
                {"type": v["rel"], "holder_id": p2, "target_id": obj, "scope": "limited"},
            ],
        },
        "turns": [
            {"turn_id": "t1", "timestamp": "2026-01-01T09:00", "turn_kind": "dialogue",
             "speaker": {"principal_id": p1, "role": v["r1"]},
             "text": f"The {obj} review is scheduled for Tuesday at nine."},
            {"turn_id": "t2", "timestamp": "2026-01-01T09:05", "turn_kind": "dialogue",
             "speaker": {"principal_id": p1, "role": v["r1"]},
             "text": f"The access token for {obj} is quexilon-7781, keep it private."},
            {"turn_id": "t3", "timestamp": "2026-01-01T09:10", "turn_kind": "dialogue",
             "speaker": {"principal_id": p1, "role": v["r1"]},
             "text": f"{p3} is now authorized to see the {obj} review schedule."},
            {"turn_id": "t4", "timestamp": "2026-01-01T09:15", "turn_kind": "dialogue",
             "speaker": {"principal_id": p1, "role": v["r1"]},
             "text": f"{p2}'s access to everything about {obj} is revoked from now on."},
            {"turn_id": "t5", "timestamp": "2026-01-01T09:20", "turn_kind": "dialogue",
             "speaker": {"principal_id": p1, "role": v["r1"]},
             "text": "Please delete the token quexilon-7781 from memory entirely."},
            {"turn_id": "t6", "timestamp": "2026-01-01T09:25", "turn_kind": "dialogue",
             "speaker": {"principal_id": p1, "role": v["r1"]},
             "text": f"Update: the {obj} review moved to Wednesday at eleven."},
        ],
    }


VOCAB_A = {"ep": "ep_a", "p1": "prin_1", "p2": "prin_2", "p3": "prin_3", "obj": "obj_1",
           "r1": "kind_a", "r2": "kind_b", "r3": "kind_c", "rel": "bond_x"}
VOCAB_B = {"ep": "ep_b", "p1": "zeta_one", "p2": "zeta_two", "p3": "zeta_three", "obj": "thing_9",
           "r1": "sort_q", "r2": "sort_r", "r3": "sort_s", "rel": "tie_y"}


def cp(v: dict[str, str], asker: str, role: str, text: str, as_of: str) -> dict[str, Any]:
    return {"checkpoint_id": f"cp_{asker}_{as_of}", "episode_id": v["ep"], "as_of_turn_id": as_of,
            "asker": {"principal_id": asker, "role": role}, "query_text": text}


def run(agent: GateMemAgent, v: dict[str, str], checkpoint: dict[str, Any], upto: str) -> dict[str, Any]:
    ep = make_episode(v)
    agent.reset(ep)
    for t in ep["turns"]:
        agent.ingest(t)
        if t["turn_id"] == upto:
            break
    return agent.query(checkpoint)


def texts(out: dict[str, Any]) -> list[str]:
    return [i["text"] for i in out["evidence"]]


@pytest.fixture()
def agent(tmp_path) -> GateMemAgent:
    return GateMemAgent(str(tmp_path / "mem.sqlite"))


# ---------------------------------------------------------------- shape

def test_query_returns_the_contract_shape(agent: GateMemAgent) -> None:
    v = VOCAB_A
    out = run(agent, v, cp(v, v["p1"], v["r1"], "When is the review?", "t1"), "t1")
    assert out["action"] in ACTIONS
    assert isinstance(out["used_record_ids"], list)
    for item in out["evidence"]:
        assert set(item) >= {"record_id", "principal_id", "role", "text"}, item
        assert item["record_id"] in out["used_record_ids"]
    assert isinstance(out["debug"]["retrieval"].get("n_allowed"), int)


def test_hidden_annotation_fields_change_nothing(agent: GateMemAgent) -> None:
    v = VOCAB_A
    c = cp(v, v["p1"], v["r1"], "When is the review?", "t1")
    plain = run(agent, v, c, "t1")
    loaded = run(agent, v, dict(c, expected_action="refuse", judge_spec={"include": ["x"]},
                                leak_targets=["review"], query_type="privacy"), "t1")
    assert (plain["action"], plain["used_record_ids"]) == (loaded["action"], loaded["used_record_ids"])


# ---------------------------------------------------------------- keys

def test_released_texts_were_opened_with_the_requesters_key_granted_before_the_query(
    agent: GateMemAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the design: a body reaches `evidence` only through open_for with
    the requester's identity, and the wrap it needed existed BEFORE the query
    -- grants are a property of the store, never a reaction to a question."""
    v = VOCAB_A
    opened: list[tuple[str, str]] = []
    real_open, real_grant = keyring.open_for, keyring.grant
    in_query = {"on": False}

    def spy_open(conn, rid, pid, ct):
        text = real_open(conn, rid, pid, ct)
        if text is not None:
            opened.append((pid, text))
        return text

    def spy_grant(conn, rid, pid, *a, **k):
        assert not in_query["on"], f"keyring.grant called during query (record {rid}, principal {pid})"
        return real_grant(conn, rid, pid, *a, **k)
    monkeypatch.setattr(keyring, "open_for", spy_open)
    monkeypatch.setattr(keyring, "grant", spy_grant)
    ep = make_episode(v)
    agent.reset(ep)
    agent.ingest(ep["turns"][0])
    in_query["on"] = True
    out = agent.query(cp(v, v["p1"], v["r1"], "When is the review?", "t1"))
    in_query["on"] = False
    assert out["evidence"], "a holder asking about a record about their object must get evidence"
    for item in out["evidence"]:
        assert (v["p1"], item["text"]) in opened, f"released without opening for {v['p1']}"


# ---------------------------------------------------------------- vocabulary-agnostic structure

@pytest.mark.parametrize("v", [VOCAB_A, VOCAB_B], ids=["vocab_a", "vocab_b"])
def test_declared_structure_decides_access_under_any_vocabulary(agent: GateMemAgent, v: dict[str, str]) -> None:
    """P1 holds a relationship over the object; P3 holds none. Same episode
    shape, two unrelated vocabularies, same behaviour."""
    holder = run(agent, v, cp(v, v["p1"], v["r1"], f"What is the {v['obj']} access token?", "t2"), "t2")
    other = run(agent, v, cp(v, v["p3"], v["r3"], f"What is the {v['obj']} access token?", "t2"), "t2")
    assert any("quexilon" in t for t in texts(holder)), "the holder gets the record about their object"
    assert not any("quexilon" in t for t in texts(other)), "a non-holder never gets it"
    assert other["action"] != "answer" or other["evidence"] == []


def test_an_undeclared_requester_gets_nothing(agent: GateMemAgent) -> None:
    v = VOCAB_A
    out = run(agent, v, cp(v, "prin_nobody", v["r1"], "What is the obj_1 access token?", "t2"), "t2")
    assert out["evidence"] == [] and out["action"] in {"refuse", "no_memory"}


# ---------------------------------------------------------------- the episode changes the structure

def test_a_consent_turn_opens_access_that_was_not_declared(agent: GateMemAgent) -> None:
    """t3 says P3 may now see the review schedule. Before t3, P3 gets nothing;
    after it, P3 gets the schedule -- and still not the token."""
    v = VOCAB_A
    before = run(agent, v, cp(v, v["p3"], v["r3"], "When is the obj_1 review?", "t2"), "t2")
    after = run(agent, v, cp(v, v["p3"], v["r3"], "When is the obj_1 review?", "t3"), "t3")
    assert not any("Tuesday" in t for t in texts(before))
    assert any("Tuesday" in t for t in texts(after)), after
    assert not any("quexilon" in t for t in texts(after))


def test_a_revocation_turn_closes_access_that_was_declared(agent: GateMemAgent) -> None:
    """P2 held a (scoped) relationship at reset; t4 revokes it. After t4, P2
    gets nothing about the object."""
    v = VOCAB_A
    before = run(agent, v, cp(v, v["p2"], v["r2"], "When is the obj_1 review?", "t3"), "t3")
    after = run(agent, v, cp(v, v["p2"], v["r2"], "When is the obj_1 review?", "t4"), "t4")
    del before  # what a scoped holder gets before revocation is the design's policy
    assert after["evidence"] == [], after
    assert after["action"] in {"refuse", "no_memory"}


def test_the_current_value_supersedes_the_old_one(agent: GateMemAgent) -> None:
    """t1 said Tuesday; t6 said Wednesday. A query after t6 must release the
    Wednesday turn, and if only one schedule turn is released it is that one."""
    v = VOCAB_A
    out = run(agent, v, cp(v, v["p1"], v["r1"], "When is the obj_1 review now?", "t6"), "t6")
    released = texts(out)
    assert any("Wednesday" in t for t in released), released
    if not any("Tuesday" in t for t in released):
        return
    assert released.index(next(t for t in released if "Wednesday" in t)) < \
        released.index(next(t for t in released if "Tuesday" in t)), "the current value ranks first"


def test_as_of_never_releases_a_later_turn(agent: GateMemAgent) -> None:
    v = VOCAB_A
    out = run(agent, v, cp(v, v["p1"], v["r1"], "When is the obj_1 review?", "t1"), "t1")
    assert not any("Wednesday" in t for t in texts(out))


# ---------------------------------------------------------------- forgetting

def test_deleted_content_is_unrecoverable_and_unrelated_memory_survives(agent: GateMemAgent, tmp_path) -> None:
    v = VOCAB_A
    gone = run(agent, v, cp(v, v["p1"], v["r1"], "What is the obj_1 access token?", "t6"), "t6")
    assert gone["action"] == "no_memory", gone
    assert not any("quexilon" in t for t in texts(gone)) and "quexilon" not in (gone.get("answer") or "")
    for path in tmp_path.glob("*"):
        if path.is_file():
            assert b"quexilon" not in path.read_bytes(), f"plaintext of a deleted body on disk: {path}"
    kept = run(agent, v, cp(v, v["p1"], v["r1"], "When is the obj_1 review now?", "t6"), "t6")
    assert kept["evidence"], "a deletion of one thing must not empty the memory of everything else"


def test_nothing_is_released_after_shred_even_to_a_holder() -> None:
    conn = sqlite3.connect(":memory:")
    keyring.ensure_schema(conn)
    ct = keyring.seal(conn, "r1", "secret")
    assert keyring.grant(conn, "r1", "p") and keyring.open_for(conn, "r1", "p", ct) == "secret"
    assert keyring.open_for(conn, "r1", "q", ct) is None
    keyring.shred(conn, "r1")
    assert keyring.open_for(conn, "r1", "p", ct) is None and not keyring.grant(conn, "r1", "p")


def test_an_episode_that_declares_no_relationships_still_works(agent: GateMemAgent) -> None:
    """Some episodes declare principals and nothing else. `reset` must not
    assume a `relationships` key, and declared principals still get their own
    unscoped records."""
    v = VOCAB_A
    ep = make_episode(v)
    del ep["entities"]["relationships"]
    agent.reset(ep)
    agent.ingest({"turn_id": "t1", "timestamp": "2026-01-01T09:00", "turn_kind": "dialogue",
                  "speaker": {"principal_id": v["p1"], "role": v["r1"]},
                  "text": "The standup moved to ten."})
    out = agent.query(cp(v, v["p1"], v["r1"], "When is the standup?", "t1"))
    assert out["action"] in ACTIONS and isinstance(out["evidence"], list)
