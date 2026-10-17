"""DESIGN.md §2.8 invariants 1-7, plus the scaffold guarantees the brief adds.

Most run in both model modes (`llm_mode`): an invariant that held only when
the model behaved would not be an invariant. Op-level checks drive the
librarian directly with hand-made ops, because the point is that the
scaffold refuses them whoever proposed them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from contracts import Outcome
from memory_system import envelope, keyring, llm, trace
from memory_system.agent import GateMemAgent
from memory_system.ops import ENVELOPE_COLUMNS, Chunk, Op
from memory_system.processor import Planned

QUESTIONS = [
    ("u_ann", "When does the thing_alpha launch window open?"),
    ("u_bo", "When does the thing_alpha launch window open?"),
    ("u_eve", "When does the thing_alpha launch window open?"),
    ("u_dee", "What is the thing_alpha passphrase?"),
    ("u_ann", "What is the thing_alpha passphrase?"),
    ("u_cy", "What is the thing_beta fee?"),
    ("u_nobody", "What is the thing_alpha passphrase?"),
]


def ingest_upto(agent: GateMemAgent, ep: dict, upto: str) -> None:
    agent.reset(ep)
    for t in ep["turns"]:
        agent.ingest(t)
        if t["turn_id"] == upto:
            return


def chunk_for(
    agent: GateMemAgent,
    speaker: str,
    text: str = "",
    about=(),
    ts: str = "2026-05-01T23:00",
    turn: str = "t_x",
    audience=(),
) -> Chunk:
    return Chunk(
        chunk_id=f"{turn}#0",
        turn_id=turn,
        timestamp=ts,
        speaker_id=speaker,
        speaker_role=agent.directory.role(speaker),
        text=text,
        kind="fact",
        about=list(about),
        suggested_audience=[speaker, *audience],
    )


def apply_op(agent: GateMemAgent, name: str, args: dict, chunk: Chunk) -> None:
    op = Op(name, args, chunk.chunk_id)
    agent.librarian.apply([Planned(op, llm_called=False, fallback=False)], [chunk])
    agent.conn.commit()


def first_record_of(agent: GateMemAgent, turn_id: str) -> str:
    row = agent.conn.execute(
        "SELECT record_id FROM records WHERE source_turn = ? ORDER BY record_id", (turn_id,)
    ).fetchone()
    assert row, f"no record from {turn_id}"
    return row[0]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 1. release only through open_for(asker)


def test_every_released_text_was_opened_for_the_asker(
    tmp_path, llm_mode, synthetic_episode, make_checkpoint, monkeypatch
) -> None:
    opened: list[tuple[str, str, str]] = []
    real_open = keyring.open_for

    def spy(conn, rid, pid, ct):
        text = real_open(conn, rid, pid, ct)
        if text is not None:
            opened.append((rid, pid, text))
        return text

    monkeypatch.setattr(keyring, "open_for", spy)
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t030")
    released = 0
    for asker, q in QUESTIONS:
        opened.clear()
        out = agent.query(make_checkpoint(asker, "r", q, "t030"))
        for item in out["evidence"]:
            released += 1
            assert (item["record_id"], asker, item["text"]) in opened, f"{asker} got text not opened for them"
            assert item["record_id"] in out["used_record_ids"]
    assert released > 0


# ---------------------------------------------------------------- 2. no grant during query


def test_grant_is_never_called_during_query(
    tmp_path, llm_mode, synthetic_episode, make_checkpoint, monkeypatch
) -> None:
    state = {"in_query": False, "grants": 0}
    real_grant = keyring.grant

    def spy(conn, rid, pid, *a, **k):
        assert not state["in_query"], f"grant({rid}, {pid}) during query"
        state["grants"] += 1
        return real_grant(conn, rid, pid, *a, **k)

    monkeypatch.setattr(keyring, "grant", spy)
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    agent.reset(synthetic_episode)
    for t in synthetic_episode["turns"]:
        agent.ingest(t)
        if t["turn_id"] in ("t003", "t006", "t009", "t010", "t020", "t030"):
            for asker, q in QUESTIONS:
                state["in_query"] = True
                agent.query(make_checkpoint(asker, "r", q, t["turn_id"]))
                state["in_query"] = False
    assert state["grants"] > 0


# ---------------------------------------------------------------- 3. grant needs authority


def test_grant_without_authority_is_rejected(tmp_path, llm_mode, synthetic_episode) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t001")
    rid = first_record_of(agent, "t001")
    # u_bo speaks, the chunk is about nobody: u_ann's authority cannot ride on it.
    c = chunk_for(agent, "u_bo", "u_eve may see it", about=["u_eve"])
    apply_op(
        agent,
        "grant",
        {"record_ids": [rid], "principal_ids": ["u_eve"], "authority": "u_ann", "chunk_id": c.chunk_id},
        c,
    )
    assert "u_eve" not in envelope.audience(agent.conn, rid)
    ev = agent.trace_events(op="grant")[-1]
    assert ev.outcome == Outcome.REJECTED and "authority" in ev.reason


def test_grant_by_an_authority_that_cannot_see_the_record_is_rejected(
    tmp_path, llm_mode, synthetic_episode
) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t003")
    rid = first_record_of(agent, "t003")  # u_cy's thing_beta record; u_eve holds nothing
    c = chunk_for(agent, "u_eve", "u_dee can see that", about=["u_dee"])
    apply_op(
        agent,
        "grant",
        {"record_ids": [rid], "principal_ids": ["u_dee"], "authority": "u_eve", "chunk_id": c.chunk_id},
        c,
    )
    assert "u_dee" not in envelope.audience(agent.conn, rid)
    assert agent.trace_events(op="grant")[-1].outcome == Outcome.REJECTED


def test_a_model_grant_with_bad_authority_is_rejected_on_the_ingest_path(
    tmp_path, synthetic_episode, monkeypatch
) -> None:
    """The same refusal when the op comes from a model reply through ingest."""
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t001")
    rid = first_record_of(agent, "t001")

    def stub(prompt, *, system="", max_tokens=None, temperature=0.0, thinking=None):
        if "# component: processor" not in system:
            return None
        cid = json.loads(prompt)["chunks"][0]["chunk_id"]
        return json.dumps(
            {
                "ops": [
                    {"op": "add", "chunk_id": cid},
                    {
                        "op": "grant",
                        "record_ids": [rid],
                        "principal_ids": ["u_eve"],
                        "authority": "u_dee",
                        "chunk_id": cid,
                    },
                ]
            }
        )

    monkeypatch.setattr(llm, "complete", stub)
    agent.ingest(synthetic_episode["turns"][1])
    assert "u_eve" not in envelope.audience(agent.conn, rid)
    assert agent.trace_events(op="grant")[-1].outcome == Outcome.REJECTED


# ---------------------------------------------------------------- 4. the envelope is frozen


@pytest.mark.parametrize("column", ["about", "valid_to", "tombstoned_at", "body", "granularity"])
def test_structure_ops_cannot_touch_envelope_columns(tmp_path, llm_mode, synthetic_episode, column) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t001")
    rid = first_record_of(agent, "t001")
    before = envelope.columns(agent.conn, "records")
    about_before = envelope.get_record(agent.conn, rid)["about"]
    c = chunk_for(agent, "u_ann")
    for name, args in [
        ("add_field", {"collection": "records", "name": column, "type": "TEXT"}),
        ("rekey", {"collection": "records", "field": column, "assignments": {rid: "x"}}),
        ("add_collection", {"name": "other", "fields": [{"name": column, "type": "TEXT"}]}),
    ]:
        n = len(agent.trace_events())
        agent.librarian.apply([Planned(Op(name, args), llm_called=False, fallback=False)], [c])
        evs = [e for e in agent.trace_events()[n:] if e.op == name]
        assert evs and evs[-1].outcome == Outcome.REJECTED, (name, column)
    assert envelope.columns(agent.conn, "records") == before
    assert envelope.get_record(agent.conn, rid)["about"] == about_before
    assert "other" not in envelope.collections(agent.conn)
    assert set(ENVELOPE_COLUMNS) <= set(before)


# ---------------------------------------------------------------- 5. views filter tombstones


def test_views_always_include_the_tombstone_filter(tmp_path, llm_mode, synthetic_episode) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t004")
    c = chunk_for(agent, "u_ann")
    apply_op(agent, "create_view", {"name": "everything", "collection": "records", "where": []}, c)
    apply_op(
        agent,
        "create_view",
        {"name": "only_dead", "collection": "records", "where": [["tombstoned_at", "IS NOT NULL"]]},
        c,
    )
    for view in ("everything", "only_dead"):
        sql = agent.conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (view,)).fetchone()[0]
        assert "tombstoned_at IS NULL" in sql
    rid = first_record_of(agent, "t004")
    apply_op(agent, "shred", {"record_ids": [rid]}, c)
    assert rid not in {r[0] for r in agent.conn.execute("SELECT record_id FROM everything")}
    assert agent.conn.execute("SELECT COUNT(*) FROM only_dead").fetchone()[0] == 0


# ---------------------------------------------------------------- 6. shredded means gone from disk


def _assert_absent(directory: Path, needles: list[bytes]) -> None:
    files = [p for p in directory.rglob("*") if p.is_file()]
    assert files
    for path in files:
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, f"{needle!r} survives in {path.name}"


def test_after_shred_no_file_holds_the_plaintext(tmp_path, llm_mode, synthetic_episode) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t003")
    secret_turn = {
        "turn_id": "t003b",
        "timestamp": "2026-05-01T08:25",
        "turn_kind": "dialogue",
        "speaker": {"principal_id": "u_cy", "role": "r_three"},
        "text": "The thing_beta vault phrase is zephyrine-4417.",
    }
    agent.ingest(secret_turn)
    rid = first_record_of(agent, "t003b")
    c = chunk_for(agent, "u_cy", ts="2026-05-01T08:26")
    # Put the secret into an added field too: shredding must clear it there.
    apply_op(
        agent, "rekey", {"collection": "records", "field": "label", "assignments": {rid: "zephyrine-4417"}}, c
    )
    assert envelope.get_record(agent.conn, rid)["label"] == "zephyrine-4417"
    apply_op(agent, "shred", {"record_ids": [rid]}, c)
    agent.close()
    _assert_absent(tmp_path, [b"zephyrine", b"4417"])


@pytest.mark.fake_llm
def test_a_deletion_turn_leaves_no_plaintext_anywhere(tmp_path, synthetic_episode, make_checkpoint) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t010")
    out = agent.query(make_checkpoint("u_cy", "r", "What is the thing_beta fee?", "t010"))
    assert out["action"] == "no_memory" and out["evidence"] == []
    agent.close()
    _assert_absent(tmp_path, [b"4,200", b"per cycle"])


# ---------------------------------------------------------------- 7. query does not write


def test_query_leaves_the_database_byte_identical(
    tmp_path, llm_mode, synthetic_episode, make_checkpoint
) -> None:
    db = tmp_path / "m.sqlite"
    agent = GateMemAgent(str(db))
    ingest_upto(agent, synthetic_episode, "t030")
    before = file_hash(db)
    for asker, q in QUESTIONS:
        agent.query(make_checkpoint(asker, "r", q, "t030"))
    assert file_hash(db) == before
    assert not list(tmp_path.glob("*-journal")) and not list(tmp_path.glob("*-wal"))


def test_query_after_its_own_flush_is_byte_identical(
    tmp_path, llm_mode, synthetic_episode, make_checkpoint
) -> None:
    db = tmp_path / "m.sqlite"
    agent = GateMemAgent(str(db), queue_batch=50)  # nothing flushes at ingest
    ingest_upto(agent, synthetic_episode, "t012")
    assert len(agent.queue) > 0
    agent.query(make_checkpoint("u_ann", "r", "When does the thing_alpha launch window open?", "t012"))
    assert len(agent.queue) == 0
    flushed = file_hash(db)
    for asker, q in QUESTIONS:
        agent.query(make_checkpoint(asker, "r", q, "t012"))
    assert file_hash(db) == flushed


def test_flushing_an_empty_queue_does_nothing(tmp_path, llm_mode, synthetic_episode, monkeypatch) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t005")
    calls = []
    monkeypatch.setattr(agent, "_process", lambda chunks, allow_hold: calls.append(chunks) or set())
    assert agent.queue.flush(agent._process) == 0 and agent.queue.flush(agent._process, final=True) == 0
    assert calls == []


def test_query_trace_event_is_held_then_written(
    tmp_path, llm_mode, synthetic_episode, make_checkpoint
) -> None:
    db = tmp_path / "m.sqlite"
    agent = GateMemAgent(str(db))
    ingest_upto(agent, synthetic_episode, "t005")
    agent.query(
        make_checkpoint("u_ann", "r", "When does the thing_alpha launch window open?", "t005", cid="c1")
    )
    held = agent.trace_events(checkpoint_id="c1")
    assert held and held[0].component == "retriever" and held[0].op_args["candidate_ids"]
    assert agent.conn.execute("SELECT COUNT(*) FROM trace WHERE checkpoint_id = 'c1'").fetchone()[0] == 0
    agent.ingest(synthetic_episode["turns"][5])
    in_table = agent.conn.execute("SELECT seq FROM trace WHERE checkpoint_id = 'c1' ORDER BY seq").fetchall()
    assert [r[0] for r in in_table] == [e.seq for e in held]
    mirror = [json.loads(line) for line in Path(trace.mirror_path(str(db))).read_text().splitlines()]
    assert [e["seq"] for e in mirror] == sorted(e["seq"] for e in mirror)
    assert {e.seq for e in agent.trace_events()} == {e["seq"] for e in mirror}


# ---------------------------------------------------------------- ordering of tightening vs widening


@pytest.mark.fake_llm
def test_tightening_overtakes_an_earlier_queued_widening(
    tmp_path, synthetic_episode, make_checkpoint
) -> None:
    """t006 (consent for u_eve) waits in the queue; a later turn revokes
    u_eve. The revocation takes the fast path, and the consent -- applied
    afterwards -- must not re-open what was closed after it."""
    ep = synthetic_episode
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, ep, "t005")
    rid = first_record_of(agent, "t001")
    agent.queue.batch = 50
    agent.ingest(ep["turns"][5])  # t006: consent, queued
    assert len(agent.queue) == 1
    agent.ingest(
        {
            "turn_id": "t006b",
            "timestamp": "2026-05-01T08:55",
            "turn_kind": "dialogue",
            "speaker": {"principal_id": "u_ann", "role": "r_one"},
            "text": "u_eve's access to everything about thing_alpha is revoked from now on.",
        }
    )
    revoke = agent.trace_events(op="revoke")
    assert revoke and revoke[-1].outcome == Outcome.APPLIED and len(agent.queue) == 1
    out = agent.query(make_checkpoint("u_eve", "r", "When does the thing_alpha launch window open?", "t006b"))
    assert out["evidence"] == []
    assert "u_eve" not in envelope.audience(agent.conn, rid)
    grant = agent.trace_events(op="grant")
    assert grant and grant[-1].seq > revoke[-1].seq and grant[-1].outcome == Outcome.REJECTED
    assert "tightening" in grant[-1].reason


def test_add_cannot_widen_beyond_the_suggested_audience(tmp_path, llm_mode, synthetic_episode) -> None:
    agent = GateMemAgent(str(tmp_path / "m.sqlite"))
    ingest_upto(agent, synthetic_episode, "t001")
    c = chunk_for(agent, "u_cy", "a private note", turn="t_y")
    apply_op(agent, "add", {"chunk_id": c.chunk_id, "audience": ["u_cy", "u_eve"]}, c)
    rid = first_record_of(agent, "t_y")
    assert envelope.audience(agent.conn, rid) == ["u_cy"]
    assert "needs a grant" in agent.trace_events(op="add")[-1].reason


def test_trace_carries_no_memory_plaintext(tmp_path, llm_mode, synthetic_episode, make_checkpoint) -> None:
    db = tmp_path / "m.sqlite"
    agent = GateMemAgent(str(db))
    ingest_upto(agent, synthetic_episode, "t030")
    agent.query(make_checkpoint("u_ann", "r", "What is the thing_alpha passphrase?", "t030"))
    agent.close()
    text = Path(trace.mirror_path(str(db))).read_text()
    for needle in ("velvet", "Monday at eight", "rehearsal step", "second shift"):
        assert needle not in text


def test_a_bad_where_clause_drops_the_clause_not_the_spec(tmp_path, monkeypatch) -> None:
    """A model search spec with one malformed `where` clause keeps its terms
    and `about`; only the clause is dropped, and the note says so. Voiding
    the whole spec sent 39% of searches on the first live run to the
    fallback, without the `about` filter."""
    from memory_system import retriever as retriever_mod
    from tests.test_contract import VOCAB_A, make_episode

    agent = GateMemAgent(str(tmp_path / "mem.sqlite"))
    ep = make_episode(VOCAB_A)
    agent.reset(ep)
    agent.ingest(ep["turns"][0])
    reply = {"search": {"where": [["project", "eq", "x"]], "terms": ["review"], "about": [VOCAB_A["p1"]]}}
    monkeypatch.setattr(retriever_mod.prompting, "ask", lambda *a, **k: (reply, True))
    spec, called, note = agent.retriever._search("When is the review?", {"principal_id": VOCAB_A["p1"]}, [])
    assert called and note.startswith("where dropped")
    assert "review" in spec.terms and spec.about == [VOCAB_A["p1"]] and spec.where == []
    # A spec that is broken beyond its where clause is still rejected whole.
    monkeypatch.setattr(retriever_mod.prompting, "ask", lambda *a, **k: ({"search": {"collections": "x"}}, True))
    _, _, note = agent.retriever._search("When is the review?", {"principal_id": VOCAB_A["p1"]}, [])
    assert note.startswith("search spec rejected")


def test_thinking_switch_is_per_component_and_off_by_default(monkeypatch) -> None:
    """MEMORY_LLM_THINKING_COMPONENTS names which calls reason; everything
    else stays a cheap classifier. The retriever's stages are addressable
    separately, and the reasoning cap replaces the classifier cap."""
    from memory_system import prompting
    from memory_system.prompting import MAX_TOKENS
    from contracts import Component

    seen: list[dict] = []

    def spy(prompt, *, system="", max_tokens=None, temperature=0.0, thinking=None):
        seen.append({"max_tokens": max_tokens, "thinking": thinking})
        return None

    monkeypatch.setattr(prompting.llm, "complete", spy)
    monkeypatch.delenv("MEMORY_LLM_THINKING_COMPONENTS", raising=False)
    prompting.ask("librarian", {"x": 1})
    assert seen[-1] == {"max_tokens": MAX_TOKENS[Component.LIBRARIAN], "thinking": False}
    monkeypatch.setenv("MEMORY_LLM_THINKING_COMPONENTS", "librarian,retriever:select")
    monkeypatch.setenv("MEMORY_LLM_THINKING_MAX_TOKENS", "2048")
    prompting.ask("librarian", {"x": 1})
    assert seen[-1] == {"max_tokens": 2048, "thinking": True}
    prompting.ask("retriever", {"stage": "search"})
    assert seen[-1]["thinking"] is False and seen[-1]["max_tokens"] == MAX_TOKENS[Component.RETRIEVER]
    prompting.ask("retriever", {"stage": "select"})
    assert seen[-1] == {"max_tokens": 2048, "thinking": True}
    prompting.ask("processor", {"x": 1})
    assert seen[-1]["thinking"] is False


def test_llm_complete_sends_enable_thinking_per_call(monkeypatch) -> None:
    """The frozen llm.py's one authorised change: `thinking` reaches the wire
    as chat_template_kwargs.enable_thinking, defaults to MEMORY_LLM_THINKING,
    and is part of the cache key."""
    import io
    import json as _json
    import urllib.request

    sent: list[dict] = []

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        sent.append(_json.loads(req.data.decode()))
        body = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return _Resp(_json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://unit.test/v1")
    monkeypatch.delenv("MEMORY_LLM_THINKING", raising=False)
    llm.new_episode()
    llm._CACHE.clear()
    assert llm.complete("q1", system="s") == "ok" and sent[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert llm.complete("q1", system="s", thinking=True) == "ok" and sent[-1]["chat_template_kwargs"] == {"enable_thinking": True}
    assert len(sent) == 2, "thinking on/off are different cache keys"
    monkeypatch.setenv("MEMORY_LLM_THINKING", "1")
    llm.complete("q2", system="s")
    assert sent[-1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_model_filters_rank_but_never_exclude(tmp_path) -> None:
    """A `where` on a field the records do not carry, or an `about` that
    matches nothing, must not empty the candidate set: the word-matched
    records still come back, with any that satisfy the filters ranked first.
    (On the first joint run a wrong `status = 'current'` filter emptied a
    third of the searches.)"""
    from memory_system.ops import Clause, SearchSpec
    from memory_system.retriever import candidates
    from tests.test_contract import VOCAB_A, make_episode

    agent = GateMemAgent(str(tmp_path / "mem.sqlite"))
    ep = make_episode(VOCAB_A)
    agent.reset(ep)
    for t in ep["turns"][:2]:
        agent.ingest(t)
    plain = candidates(agent.conn, SearchSpec(terms=["review", "token"]), as_of=None)
    assert plain, "the word-matched records exist"
    # A field no record has a value for: every record is filtered out strictly.
    envelope.add_field(agent.conn, "records", "status", "TEXT", at="2026-01-01T09:30")
    bad = candidates(agent.conn, SearchSpec(terms=["review", "token"], where=[Clause("status", "=", "current")]),
                     as_of=None)
    assert {c.record_id for c in bad} == {c.record_id for c in plain}
    # An `about` nothing matches behaves the same way.
    off = candidates(agent.conn, SearchSpec(terms=["review", "token"], about=["nobody_at_all"]), as_of=None)
    assert {c.record_id for c in off} == {c.record_id for c in plain}
    # Records that DO satisfy `about` rank ahead of those that do not.
    a_rec = next(c for c in plain if c.about)
    first = candidates(agent.conn, SearchSpec(terms=["review", "token"], about=list(a_rec.about)), as_of=None)
    assert first and first[0].about and set(first[0].about) & set(a_rec.about)


def test_librarian_consults_the_model_once_per_batch_and_honours_each_index(tmp_path, monkeypatch) -> None:
    """Three structure proposals in one apply(): ONE model call, decided by
    index -- one executed as corrected, one declined, one left undecided and
    run as proposed. The old single-object reply form still decides index 0.
    (Per-op calls were a third of all endpoint time on the first joint run.)"""
    from memory_system import librarian as lib_mod
    from tests.test_contract import VOCAB_A, make_episode

    agent = GateMemAgent(str(tmp_path / "mem.sqlite"))
    ep = make_episode(VOCAB_A)
    agent.reset(ep)
    agent.ingest(ep["turns"][0])
    chunk = Chunk(chunk_id="t9#0", turn_id="t9", timestamp="2026-01-01T10:00", speaker_id=VOCAB_A["p1"],
                  speaker_role=VOCAB_A["r1"], text="x", kind="fact")
    proposals = [
        Op("add_field", {"collection": "records", "name": "topic", "type": "TEXT"}, "t9#0"),
        Op("add_field", {"collection": "records", "name": "value", "type": "TEXT"}, "t9#0"),
        Op("add_field", {"collection": "records", "name": "status", "type": "TEXT"}, "t9#0"),
    ]
    planned = [Planned(op=o, llm_called=True, fallback=False) for o in proposals]
    calls: list[dict] = []
    reply = [{"index": 0, "op": "add_field", "collection": "records", "name": "subject", "type": "TEXT"},
             {"index": 1, "decline": True, "reason": "too generic"}]

    def ask(component, payload, *, prompts_dir=None):
        calls.append(payload)
        return reply, True

    monkeypatch.setattr(lib_mod.prompting, "ask", ask)
    agent.librarian.apply(planned, [chunk])
    assert len(calls) == 1 and [p["index"] for p in calls[0]["proposed"]] == [0, 1, 2]
    cols = envelope.columns(agent.conn, "records")
    assert "subject" in cols and "topic" not in cols, "index 0 ran as corrected by the model"
    assert "value" not in cols, "index 1 was declined"
    assert "status" in cols, "index 2, undecided, ran as proposed"
    events = [e for e in trace.events(agent.conn) if e.component == "librarian" and e.op == "add_field"]
    assert [e.outcome for e in events[-3:]] == ["applied", "rejected", "applied"]
    # Old single-object reply: decides proposal 0 only.
    reply[:] = []
    single = {"decline": True, "reason": "nope"}
    monkeypatch.setattr(lib_mod.prompting, "ask", lambda c, p, *, prompts_dir=None: (single, True))
    agent.librarian.apply([Planned(op=Op("add_field", {"collection": "records", "name": "kind_x", "type": "TEXT"},
                                         "t9#0"), llm_called=True, fallback=False)], [chunk])
    assert "kind_x" not in envelope.columns(agent.conn, "records")
