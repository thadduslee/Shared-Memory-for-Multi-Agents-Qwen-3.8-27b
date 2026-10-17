"""Held-out adaptivity checks -- run by the harness, never copied into a workspace.

The contract tests in the workspace use plain statements ("X is now authorized",
"...is revoked"), so a keyword rule passes them and a memory never has to READ
its episode. These checks phrase the same structural changes the way people
do -- without the obvious keywords, in vocabularies the build has never seen --
and run with the memory's model call ENABLED against the live endpoint. The
Developer sees only which checks failed, never this source: they measure
whether the memory adapts, and a list of phrasings to match would defeat that.

Skipped (not passed) when no MEMORY_LLM_BASE_URL is configured.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

if not os.environ.get("MEMORY_LLM_BASE_URL", "").strip():
    pytest.skip("no memory-model endpoint configured", allow_module_level=True)

from memory_system.agent import GateMemAgent

VOCABS = [
    {"ep": "ep_k1", "p1": "orla_vance", "p2": "tomas_reyk", "p3": "ines_qadir", "obj": "halcyon",
     "r1": "steward", "r2": "liaison", "r3": "observer", "rel": "keeps"},
    {"ep": "ep_k2", "p1": "bex_ondru", "p2": "cato_mell", "p3": "dara_sion", "obj": "marrow_line",
     "r1": "warden", "r2": "courier", "r3": "guest", "rel": "tends"},
]


def episode(v: dict[str, str], extra: list[tuple[str, str]]) -> dict[str, Any]:
    """P1, P2 hold `rel` over `obj`; P3 holds nothing. Base facts, then `extra`
    turns as (speaker principal, text)."""
    turns = [
        (v["p1"], f"Planning note: the {v['obj']} review is Tuesday at nine in room four."),
        (v["p1"], f"The vault code for {v['obj']} is kestrel-4471; keep that between the two of us."),
    ] + extra
    roles = {v["p1"]: v["r1"], v["p2"]: v["r2"], v["p3"]: v["r3"]}
    return {
        "episode_id": v["ep"],
        "entities": {
            "principals": [{"principal_id": p, "role": roles[p], "display_name": p}
                           for p in (v["p1"], v["p2"], v["p3"])],
            "relationships": [{"type": v["rel"], "holder_id": v["p1"], "target_id": v["obj"]},
                              {"type": v["rel"], "holder_id": v["p2"], "target_id": v["obj"]}],
        },
        "turns": [{"turn_id": f"t{i + 1}", "timestamp": f"2026-02-0{1 + i // 9}T0{i % 9}:00",
                   "turn_kind": "dialogue", "speaker": {"principal_id": s, "role": roles[s]}, "text": x}
                  for i, (s, x) in enumerate(turns)],
    }


def ask(ep: dict[str, Any], asker: str, role: str, text: str, upto: int) -> dict[str, Any]:
    agent = GateMemAgent(":memory:")
    agent.reset(ep)
    for t in ep["turns"][:upto]:
        agent.ingest(t)
    return agent.query({"checkpoint_id": "c", "episode_id": ep["episode_id"],
                        "as_of_turn_id": ep["turns"][upto - 1]["turn_id"],
                        "asker": {"principal_id": asker, "role": role}, "query_text": text})


def released(out: dict[str, Any]) -> str:
    return " || ".join(i["text"] for i in out["evidence"])


CONSENT = [
    "Going forward it's fine to loop {p3} in on when the {obj} review happens.",
    "{p3} should be kept in the picture about the timing of the {obj} review from now on.",
]
REVOKE = [
    "Please stop sharing anything about {obj} with {p2}; that arrangement has ended.",
    "{p2} is off the {obj} work as of today, so nothing more goes to them.",
]
SUPERSEDE = [
    "Scratch the earlier plan: the {obj} review happens Wednesday at eleven instead.",
    "Heads up, the {obj} review got pushed to Wednesday at eleven.",
]


@pytest.mark.parametrize("vi", [0, 1], ids=["v1", "v2"])
@pytest.mark.parametrize("pi", [0, 1], ids=["p1", "p2"])
def test_consent_by_paraphrase(vi: int, pi: int) -> None:
    v = VOCABS[vi]
    ep = episode(v, [(v["p1"], CONSENT[pi].format(**v))])
    before = ask(ep, v["p3"], v["r3"], f"When is the {v['obj']} review?", 2)
    after = ask(ep, v["p3"], v["r3"], f"When is the {v['obj']} review?", 3)
    assert "Tuesday" not in released(before)
    assert "Tuesday" in released(after)
    assert "kestrel" not in released(after), "consent to the schedule is not consent to everything"


@pytest.mark.parametrize("vi", [0, 1], ids=["v1", "v2"])
@pytest.mark.parametrize("pi", [0, 1], ids=["p1", "p2"])
def test_revocation_by_paraphrase(vi: int, pi: int) -> None:
    v = VOCABS[vi]
    ep = episode(v, [(v["p1"], REVOKE[pi].format(**v))])
    before = ask(ep, v["p2"], v["r2"], f"When is the {v['obj']} review?", 2)
    after = ask(ep, v["p2"], v["r2"], f"When is the {v['obj']} review?", 3)
    assert "Tuesday" in released(before), "a declared holder sees the schedule before revocation"
    assert "Tuesday" not in released(after) and "kestrel" not in released(after)


@pytest.mark.parametrize("vi", [0, 1], ids=["v1", "v2"])
@pytest.mark.parametrize("pi", [0, 1], ids=["p1", "p2"])
def test_supersession_by_paraphrase(vi: int, pi: int) -> None:
    v = VOCABS[vi]
    ep = episode(v, [(v["p1"], SUPERSEDE[pi].format(**v))])
    out = ask(ep, v["p1"], v["r1"], f"When is the {v['obj']} review now?", 3)
    texts = [i["text"] for i in out["evidence"]]
    assert any("Wednesday" in t for t in texts), texts
    first_schedule = next((t for t in texts if "Wednesday" in t or "Tuesday" in t), "")
    assert "Wednesday" in first_schedule, "the current value must come first"


# ----------------------------------------------------------------------------
# Reference by NAME. Episodes declare ids (`proj_halcyon`, `prin_orla_vance`)
# but their turns say "Halcyon" and "Orla". A memory that only matches declared
# ids verbatim never connects a turn to what it is about. Two objects whose
# names nest ("Halcyon" / "Halcyonmark") must not be confused.
# ----------------------------------------------------------------------------

NAMED = [
    {"ep": "ep_n1", "a": "prin_orla_vance", "b": "prin_tomas_reyk", "c": "prin_dara_sion",
     "d": "prin_scribe_one", "dn": "Pell Scribe",
     "an": "Orla Vance", "bn": "Tomas Reyk", "cn": "Dara Sion",
     "o1": "proj_halcyon", "o2": "proj_halcyonmark", "n1": "Halcyon", "n2": "Halcyonmark", "rel": "member_of"},
    {"ep": "ep_n2", "a": "usr_bex_ondru", "b": "usr_cato_mell", "c": "usr_ines_qadir",
     "d": "usr_notes_bot", "dn": "Quill Notes",
     "an": "Bex Ondru", "bn": "Cato Mell", "cn": "Ines Qadir",
     "o1": "acct_marrow", "o2": "acct_marrowline", "n1": "Marrow", "n2": "Marrowline", "rel": "works_on"},
]


def named_episode(v: dict[str, str], extra: list[tuple[str, str]] = ()) -> dict[str, Any]:
    # The facts are spoken by a fourth principal who holds nothing, so a
    # speaker-owns-their-turn rule cannot stand in for resolving the name.
    turns = [
        (v["d"], f"{v['n1']} launches on March 3; that's locked."),
        (v["d"], f"{v['n2']} has a budget of 40,000 for the quarter."),
    ] + list(extra)
    roles = {v["a"]: "member", v["b"]: "member", v["c"]: "visitor", v["d"]: "clerk"}
    names = {v["a"]: v["an"], v["b"]: v["bn"], v["c"]: v["cn"], v["d"]: v["dn"]}
    return {
        "episode_id": v["ep"],
        "entities": {
            "principals": [{"principal_id": p, "role": roles[p], "display_name": names[p]} for p in roles],
            "relationships": [{"type": v["rel"], "principal_id": v["a"], "target_id": v["o1"]},
                              {"type": v["rel"], "principal_id": v["b"], "target_id": v["o2"]}],
        },
        "turns": [{"turn_id": f"t{i + 1}", "timestamp": f"2026-03-01T0{i}:00", "turn_kind": "dialogue",
                   "speaker": {"principal_id": s, "role": roles[s]}, "text": x}
                  for i, (s, x) in enumerate(turns)],
    }


@pytest.mark.parametrize("vi", [0, 1], ids=["v1", "v2"])
def test_reference_by_name_reaches_the_holder(vi: int) -> None:
    v = NAMED[vi]
    ep = named_episode(v)
    out = ask(ep, v["a"], "member", f"When does {v['n1']} launch?", 2)
    assert "March 3" in released(out)


@pytest.mark.parametrize("vi", [0, 1], ids=["v1", "v2"])
def test_reference_by_name_does_not_cross_nested_names(vi: int) -> None:
    v = NAMED[vi]
    ep = named_episode(v)
    out = ask(ep, v["a"], "member", f"What is the {v['n2']} budget?", 2)
    assert "40,000" not in released(out)
    own = ask(ep, v["b"], "member", f"What is the {v['n2']} budget?", 2)
    assert "40,000" in released(own)


@pytest.mark.parametrize("vi", [0, 1], ids=["v1", "v2"])
def test_consent_naming_people_by_display_name(vi: int) -> None:
    v = NAMED[vi]
    first = v["cn"].split()[0]
    ep = named_episode(v, [(v["a"], f"{first} can be told when {v['n1']} launches.")])
    before = ask(ep, v["c"], "visitor", f"When does {v['n1']} launch?", 2)
    after = ask(ep, v["c"], "visitor", f"When does {v['n1']} launch?", 3)
    assert "March 3" not in released(before)
    assert "March 3" in released(after)
    assert "40,000" not in released(after)
