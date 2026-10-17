"""The full pipeline with the deterministic fake model: front door -> queue
-> processor -> librarian -> store, and the retriever, on the contract
episode and a 30-turn synthetic one. These check that the model-driven paths
DO what the trace says they did: a supersession, a consent that widened an
audience, a revocation that narrowed one, a deletion that shredded, and a
structure change motivated by an integrity signal.
"""

from __future__ import annotations

import json

import pytest

from contracts import Outcome
from memory_system import envelope, fake_llm, keyring, llm
from memory_system.agent import GateMemAgent
from memory_system.ops import Chunk, Op
from memory_system.processor import Planned
from tests.test_contract import VOCAB_A, cp, make_episode

pytestmark = pytest.mark.fake_llm

STRUCTURE = {"add_field", "add_collection", "add_index", "create_view", "rekey"}


def record_from(agent: GateMemAgent, turn_id: str) -> str:
    return agent.conn.execute(
        "SELECT record_id FROM records WHERE source_turn = ? ORDER BY record_id", (turn_id,)
    ).fetchone()[0]


def applied(agent: GateMemAgent, op: str) -> list:
    return [e for e in agent.trace_events(op=op) if e.outcome == Outcome.APPLIED]


def test_fake_is_installed_and_charges_usage(tmp_path) -> None:
    assert fake_llm.installed() and llm.enabled()
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ep = make_episode(VOCAB_A)
    agent.reset(ep)
    agent.ingest(ep["turns"][0])
    out = agent.query(cp(VOCAB_A, "prin_1", "kind_a", "When is the review?", "t1"))
    assert out["debug"]["memory_llm"]["calls"] >= 3
    assert all(e.llm_called for e in agent.trace_events() if e.component in ("front_door", "processor"))


def test_contract_episode_end_to_end(tmp_path) -> None:
    v = VOCAB_A
    ep = make_episode(v)
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(ep)
    for t in ep["turns"]:
        agent.ingest(t)
    schedule, token = record_from(agent, "t1"), record_from(agent, "t2")

    grant = applied(agent, "grant")
    assert grant and schedule in grant[-1].record_ids and grant[-1].op_args["principal_ids"] == [v["p3"]]
    assert v["p3"] in envelope.audience(agent.conn, schedule)
    assert token not in grant[-1].record_ids

    shred = applied(agent, "shred")
    assert shred and shred[-1].record_ids == [token]
    rec = envelope.get_record(agent.conn, token)
    assert rec["tombstoned_at"] and rec["body"] is None and keyring.holders(agent.conn, token) == []

    sup = applied(agent, "supersede")
    assert sup and sup[-1].record_ids[1] == schedule
    old = envelope.get_record(agent.conn, schedule)
    assert old["valid_to"] and old["superseded_by"] == sup[-1].record_ids[0]

    assert any(
        e.op in STRUCTURE and e.outcome == Outcome.APPLIED and e.component == "librarian"
        for e in agent.trace_events()
    )

    out = agent.query(cp(v, v["p1"], v["r1"], "When is the obj_1 review now?", "t6"))
    assert [i["text"] for i in out["evidence"]][0].startswith("Update:")
    assert not any("Tuesday" in i["text"] for i in out["evidence"])


def test_synthetic_episode_end_to_end(tmp_path, synthetic_episode, make_checkpoint) -> None:
    ep = synthetic_episode
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(ep)
    audience_at: dict[str, dict[str, list[str]]] = {}
    for t in ep["turns"]:
        agent.ingest(t)
        if t["turn_id"] in ("t005", "t006", "t008", "t009"):
            audience_at[t["turn_id"]] = {
                r: envelope.audience(agent.conn, r) for r in envelope.live_ids(agent.conn, current_only=False)
            }
    window = record_from(agent, "t001")

    # consent widened an audience
    assert "u_eve" not in audience_at["t005"][window] and "u_eve" in audience_at["t006"][window]
    # a correction superseded it, and the author's distribution carried over
    newer = envelope.get_record(agent.conn, window)["superseded_by"]
    assert newer and "u_eve" in audience_at["t008"][newer]
    # the revocation narrowed both versions
    assert "u_bo" in audience_at["t008"][window] and "u_bo" in audience_at["t008"][newer]
    assert "u_bo" not in audience_at["t009"][window] and "u_bo" not in audience_at["t009"][newer]
    rev = applied(agent, "revoke")[-1]
    assert rev.op_args["scope"] == ["thing_alpha"] and window in rev.record_ids
    # the qualified holder never had it
    assert "u_dee" not in envelope.audience(agent.conn, newer)
    # the deletion shredded
    fee = record_from(agent, "t003")
    assert applied(agent, "shred")[-1].record_ids == [fee]
    assert keyring.holders(agent.conn, fee) == []
    # structure changed, motivated by an integrity signal, and is traced as such
    structure = [e for e in agent.trace_events() if e.op in STRUCTURE and e.outcome == Outcome.APPLIED]
    assert {e.op for e in structure} >= {"add_field", "rekey"}
    assert all(e.signal_kind == "integrity:schema_mismatch" and e.component == "librarian" for e in structure)
    assert "subject" in envelope.fields(agent.conn, "records")
    assert any(e.signal_kind == "integrity:schema_mismatch" for e in agent.trace_events(op="signal"))

    def ask(asker: str, q: str) -> dict:
        return agent.query(make_checkpoint(asker, "r", q, "t030"))

    eve = ask("u_eve", "When does the thing_alpha launch window open?")
    assert eve["action"] == "answer" and eve["evidence"][0]["text"].startswith("Update:")
    assert eve["evidence"][0]["principal_id"] == "u_ann"  # attributed to its speaker
    assert ask("u_bo", "When does the thing_alpha launch window open?")["evidence"] == []
    assert ask("u_cy", "What is the thing_beta fee?")["action"] == "no_memory"
    assert ask("u_dee", "What is the thing_alpha passphrase?")["action"] == "refuse"
    own = ask("u_ann", "What is the thing_alpha passphrase?")
    assert [i["text"] for i in own["evidence"]] == ["The thing_alpha passphrase is velvet-9921."]


def test_restrict_to_summary_releases_the_summary_redacted(
    tmp_path, synthetic_episode, make_checkpoint
) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(synthetic_episode)
    for t in synthetic_episode["turns"][:4]:
        agent.ingest(t)
    window = record_from(agent, "t001")
    c = Chunk("t_z#0", "t_z", "2026-05-01T08:45", "u_ann", "r_one", "", "fact")
    ops = [
        Op(
            "derive",
            {
                "source_record_ids": [window],
                "text": "The thing_alpha launch window is early in the week.",
                "granularity": "summary",
            },
            c.chunk_id,
        ),
        Op(
            "restrict",
            {"record_ids": [window], "principal_ids": ["u_bo"], "granularity": "summary"},
            c.chunk_id,
        ),
    ]
    agent.librarian.apply([Planned(o, False, False) for o in ops], [c])
    assert "u_bo" not in envelope.audience(agent.conn, window)
    out = agent.query(make_checkpoint("u_bo", "r", "When does the thing_alpha launch window open?", "t004"))
    assert out["action"] == "answer_redacted"
    assert [i["text"] for i in out["evidence"]] == ["The thing_alpha launch window is early in the week."]
    full = agent.query(make_checkpoint("u_ann", "r", "When does the thing_alpha launch window open?", "t004"))
    assert any("Monday" in i["text"] for i in full["evidence"])


def test_expire_withdraws_at_the_stated_time(tmp_path, synthetic_episode, make_checkpoint) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(synthetic_episode)
    turns = synthetic_episode["turns"]
    agent.ingest(turns[0])
    window = record_from(agent, "t001")
    c = Chunk("t_e#0", "t_e", turns[0]["timestamp"], "u_ann", "r_one", "", "consent", about=["u_eve"])
    ops = [
        Op("grant", {"record_ids": [window], "principal_ids": ["u_eve"], "authority": "u_ann"}, c.chunk_id),
        Op(
            "expire",
            {"record_ids": [window], "principal_ids": ["u_eve"], "at": turns[2]["timestamp"]},
            c.chunk_id,
        ),
    ]
    agent.librarian.apply([Planned(o, False, False) for o in ops], [c])
    q = "When does the thing_alpha launch window open?"
    agent.ingest(turns[1])
    assert agent.query(make_checkpoint("u_eve", "r", q, "t002"))["evidence"]
    agent.ingest(turns[2])
    assert agent.query(make_checkpoint("u_eve", "r", q, "t003"))["evidence"] == []
    assert "u_eve" not in envelope.audience(agent.conn, window)


def test_a_paraphrasing_front_door_reply_is_rejected(tmp_path, synthetic_episode, monkeypatch) -> None:
    def stub(prompt, *, system="", max_tokens=None, temperature=0.0, thinking=None):
        if "# component: front_door" in system:
            return json.dumps({"chunks": [{"text": "Launch is Monday.", "kind": "fact"}]})
        return fake_llm.complete(prompt, system=system, max_tokens=max_tokens)

    monkeypatch.setattr(llm, "complete", stub)
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(synthetic_episode)
    agent.ingest(synthetic_episode["turns"][0])
    fd = agent.trace_events(component="front_door")
    assert [e.outcome for e in fd] == [Outcome.REJECTED, Outcome.FALLBACK]
    assert "verbatim" in fd[0].reason
    text = keyring.open_for(
        agent.conn,
        record_from(agent, "t001"),
        "u_ann",
        envelope.get_record(agent.conn, record_from(agent, "t001"))["body"],
    )
    assert text == synthetic_episode["turns"][0]["text"]


def test_out_of_vocabulary_ops_are_rejected_and_the_chunk_still_covered(
    tmp_path, synthetic_episode, monkeypatch
) -> None:
    def stub(prompt, *, system="", max_tokens=None, temperature=0.0, thinking=None):
        if "# component: processor" in system:
            return json.dumps(
                {
                    "ops": [
                        {"op": "drop_table", "name": "records"},
                        {"op": "add_field", "collection": "records", "name": "valid_to", "type": "TEXT"},
                    ]
                }
            )
        return fake_llm.complete(prompt, system=system, max_tokens=max_tokens)

    monkeypatch.setattr(llm, "complete", stub)
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(synthetic_episode)
    agent.ingest(synthetic_episode["turns"][0])
    rejected = [e for e in agent.trace_events(component="processor") if e.outcome == Outcome.REJECTED]
    assert len(rejected) == 2
    adds = agent.trace_events(op="add")
    assert adds and adds[-1].outcome == Outcome.FALLBACK and "uncovered" in adds[-1].reason


def test_budget_exhaustion_degrades_to_the_fallbacks(
    tmp_path, synthetic_episode, make_checkpoint, monkeypatch
) -> None:
    monkeypatch.setenv("MEMORY_LLM_CALL_BUDGET", "12")
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(synthetic_episode)
    for t in synthetic_episode["turns"]:
        agent.ingest(t)
    out = agent.query(make_checkpoint("u_ann", "r", "What is the thing_alpha passphrase?", "t030"))
    assert out["action"] in ("answer", "answer_redacted", "refuse", "no_memory")
    usage = out["debug"]["memory_llm"]
    assert usage["calls"] == 12 and usage["refused_over_budget"] > 0
    late = [e for e in agent.trace_events(component="front_door") if e.turn_id == "t030"]
    assert late and all(e.outcome == Outcome.FALLBACK and not e.llm_called for e in late)
