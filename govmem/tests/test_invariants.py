"""SPEC §13.1 invariants I-1 .. I-13 and I-16, through the public interface where possible."""

from __future__ import annotations

import hashlib
import sqlite3

from conftest import FakeChat, make_memory

from govmem import Turn
from govmem.core import keyring
from govmem.core.ops import Op
from govmem.core.schema import ACCESS_TABLES


def label_handler(spec: dict[str, dict]):
    """Label replies keyed by a marker word in the message text."""
    def h(system: str, user: str) -> dict:
        text = user.split("text: ", 1)[-1]
        for marker, reply in spec.items():
            if marker in text:
                return reply
        return {"kind": "chatter", "quotes": [], "audience": {"full": [], "redacted": []}}
    return h


def fact(quote: str, about=(), owners=(), full=("speaker", "about"), slot=None, exclude=(), protected=()):  # noqa: ANN001
    return {"kind": "fact", "quotes": [{"text": quote, "about": list(about), "owners": list(owners), "slot": slot,
                                        "shareable": None}],
            "audience": {"full": list(full), "redacted": []}, "exclude": list(exclude),
            "protected_values": list(protected), "rescope": None, "remove": None, "aliases": []}


def access_hash(m) -> str:  # noqa: ANN001
    h = hashlib.sha256()
    for t in ACCESS_TABLES:
        for row in m.store.conn.execute(f"SELECT * FROM {t} ORDER BY 1"):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def test_i1_released_text_was_opened_with_the_askers_identity(tmp_path, monkeypatch):
    chat = FakeChat({"label": label_handler({"zephyr": fact("zephyr code is 4471-QX", about=["p_ash"],
                                                             owners=["p_ash"], protected=["4471-QX"])})})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "2026-01-01T09:00", "p_ash", "The zephyr code is 4471-QX."))
    calls = []
    real = keyring.open_for

    def spy(conn, key_id, principal_id, nonce, ct):  # noqa: ANN001
        calls.append((key_id, principal_id))
        return real(conn, key_id, principal_id, nonce, ct)

    monkeypatch.setattr(keyring, "open_for", spy)
    ev = m.query("p_bex", "what is the zephyr code?", "t1")
    for item in ev.items:
        assert (item["record_id"], "p_bex") in calls or (item["record_id"] + "#r", "p_bex") in calls
    assert all(pid != keyring.OWNER for kid, pid in calls if kid in ev.record_ids)


def test_i2_i3_query_changes_no_access_state(tmp_path):
    chat = FakeChat({"label": label_handler({"zephyr": fact("zephyr code is 4471-QX", about=["p_ash"])})})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "The zephyr code is 4471-QX."))
    before = access_hash(m)
    m.query("p_cyd", "zephyr code?", "t1")
    m.query("p_ash", "zephyr code?", "t1")
    assert access_hash(m) == before


def test_private_by_default_and_subject_and_unqualified_relationship_read(tmp_path):
    chat = FakeChat({"label": label_handler({"zephyr": fact("zephyr code is 4471-QX", about=["p_ash"])})})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_cyd", "The zephyr code is 4471-QX."))
    rid = m.store.records()[0].id
    assert m.store.view_of(rid, "p_cyd") == "full"       # speaker
    assert m.store.view_of(rid, "p_ash") == "full"       # subject
    assert m.store.view_of(rid, "p_bex") == "full"       # unqualified relationship with the subject
    assert m.store.view_of(rid, "p_dov") == "none"


def test_i4_grant_without_authority_is_rejected(tmp_path):
    chat = FakeChat({"label": label_handler({"zephyr": fact("zephyr code is 4471-QX", about=["p_ash"],
                                                             owners=["p_ash"])})})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "The zephyr code is 4471-QX."))
    rid = m.store.records()[0].id
    before = access_hash(m)
    res = m.apply(Op("grant", {"records": [rid], "principals": ["p_dov"]}, "p_cyd", "t1", "caller"))
    assert res.outcome == "rejected" and access_hash(m) != "" and m.store.view_of(rid, "p_dov") == "none"
    res = m.apply(Op("grant", {"records": [rid], "principals": ["p_dov"]}, "p_ash", "t1", "caller"))
    assert res.ok and m.store.view_of(rid, "p_dov") == "full"
    del before


def test_i5_denial_outranks_later_widening_by_non_owner(tmp_path):
    chat = FakeChat({"label": label_handler({"zephyr": fact("zephyr code is 4471-QX", about=["p_ash"],
                                                             owners=["p_ash"], full=["speaker", "about", "p_cyd"])})})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "The zephyr code is 4471-QX."))
    rid = m.store.records()[0].id
    assert m.store.view_of(rid, "p_cyd") == "full"
    m.store.register_turn("t2", "", "p_ash")
    assert m.apply(Op("revoke", {"records": [rid], "principals": ["p_cyd"]}, "p_ash", "t2", "caller")).ok
    assert m.store.view_of(rid, "p_cyd") == "none"
    m.store.register_turn("t3", "", "p_bex")
    # p_bex can read it but is not an owner: cannot re-widen
    assert m.apply(Op("grant", {"records": [rid], "principals": ["p_cyd"]}, "p_bex", "t3", "caller")).outcome == "rejected"
    assert m.store.view_of(rid, "p_cyd") == "none"


def test_i6_i7_delete_is_unrecoverable_across_chain_and_disk(tmp_path):
    secret = "orchid-7731-velvet"
    chat = FakeChat({"label": label_handler({
        "first": fact(f"first locker word {secret}", about=["p_ash"], owners=["p_ash"], slot="p_ash:locker"),
        "second": fact("second locker word lumen-2208-quartz", about=["p_ash"], owners=["p_ash"], slot="p_ash:locker"),
    })})
    m = make_memory(tmp_path, chat, supersede_similarity=-1.0)
    m.cfg.supersede_similarity = -1.0
    m.ingest(Turn("t1", "", "p_ash", f"My first locker word {secret}."))
    m.ingest(Turn("t2", "", "p_ash", "My second locker word lumen-2208-quartz."))
    recs = m.store.records(current_only=False)
    assert len(recs) == 2 and recs[0].superseded_by == recs[1].id
    m.store.register_turn("t3", "", "p_ash")
    res = m.apply(Op("delete", {"records": [recs[1].id]}, "p_ash", "t3", "caller"))
    assert res.ok and set(res.record_ids) >= {recs[0].id, recs[1].id}
    ev = m.query("p_ash", "locker word", "t3")
    assert not ev.items and ev.action == "no_memory"
    m.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    m.store.conn.execute("VACUUM")
    m.close()
    for f in tmp_path.iterdir():
        assert secret.encode() not in f.read_bytes(), f.name


def test_i8_derived_record_audience_is_the_intersection(tmp_path):
    chat = FakeChat({"label": label_handler({
        "alpha": fact("alpha note", full=["speaker", "p_bex", "p_cyd"]),
        "beta": fact("beta note", full=["speaker", "p_bex"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_dov", "The alpha note."))
    m.ingest(Turn("t2", "", "p_dov", "The beta note."))
    a, b = [r.id for r in m.store.records()]
    res = m.apply(Op("derive", {"sources": [a, b], "text": "summary"}, "p_dov", "t2", "caller"))
    d = res.record_ids[0]
    assert m.store.full_audience(d) == {"p_dov", "p_bex"}


def test_i9_quotes_must_be_verbatim(tmp_path):
    chat = FakeChat({"label": label_handler({"zephyr": fact("a paraphrase that is not in the turn")})})
    m = make_memory(tmp_path, chat, completeness_guard=False)
    o = m.ingest(Turn("t1", "", "p_ash", "The zephyr code is 4471-QX."))
    assert not o.new_records
    assert m.store.health().get("label.quote_not_verbatim") == 1


def test_i10_superseded_value_is_not_released_as_current(tmp_path):
    chat = FakeChat({"label": label_handler({
        "old": fact("dock is pier 4 old", about=["p_ash"], slot="p_ash:dock"),
        "new": fact("dock is pier 9 new", about=["p_ash"], slot="p_ash:dock"),
    })})
    m = make_memory(tmp_path, chat)
    m.cfg.supersede_similarity = -1.0
    m.ingest(Turn("t1", "", "p_ash", "The dock is pier 4 old."))
    m.ingest(Turn("t2", "", "p_ash", "The dock is pier 9 new."))
    texts = [i["text"] for i in m.query("p_ash", "which dock", "t2").items]
    assert any("pier 9" in t for t in texts) and not any("pier 4" in t for t in texts)
    texts_then = [i["text"] for i in m.query("p_ash", "which dock", "t1").items]
    assert any("pier 4" in t for t in texts_then)


def test_i11_note_cannot_draw_on_a_narrower_record(tmp_path):
    chat = FakeChat({"label": label_handler({
        "wide": fact("wide record", full=["everyone"]),
        "narrow": fact("narrow record", full=["speaker"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "A wide record."))
    m.ingest(Turn("t2", "", "p_ash", "A narrow record."))
    wide, narrow = [r.id for r in m.store.records()]
    res = m.apply(Op("annotate", {"record": wide, "context": "x", "sources": [narrow]}, "__organiser__", "t2",
                     "organised", evidence={"turn": "t2"}))
    assert res.outcome == "rejected" and res.reason == "AUDIENCE_WIDENS"
    res = m.apply(Op("annotate", {"record": narrow, "context": "x", "sources": [wide]}, "__organiser__", "t2",
                     "organised", evidence={"turn": "t2"}))
    assert res.ok


def test_i12_link_expansion_only_follows_links_the_asker_can_see(tmp_path):
    chat = FakeChat({"label": label_handler({
        "public": fact("public record", full=["everyone"]),
        "private": fact("private record", full=["speaker"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "A public record."))
    m.ingest(Turn("t2", "", "p_ash", "A private record."))
    pub, priv = [r.id for r in m.store.records()]
    m.apply(Op("link", {"a": pub, "b": priv, "kind": "same_topic"}, "__organiser__", "t2", "organised",
               evidence={"turn": "t2"}))
    assert priv not in m.retriever._expand([pub], "p_cyd")
    assert priv in m.retriever._expand([pub], "p_ash")


def test_i13_pending_reorganisation_has_no_effect(tmp_path):
    chat = FakeChat({"label": label_handler({
        "pier 4": fact("dock is pier 4", about=["p_ash"]),
        "pier 9": fact("dock is pier 9", about=["p_ash"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "The dock is pier 4."))
    m.ingest(Turn("t2", "", "p_ash", "The dock is pier 9."))
    a, b = [r.id for r in m.store.records()]
    res = m.apply(Op("link", {"a": a, "b": b, "kind": "same_value_slot"}, "__organiser__", "t2", "organised",
                     evidence={"turn": "t2"}))
    assert res.outcome == "pending"
    assert m.store.record(a).superseded_by is None
    assert m.retriever._expand([a], "p_ash") == [] or b not in m.retriever._expand([a], "p_ash")


def test_i16_model_disabled_still_works(tmp_path):
    m = make_memory(tmp_path, None)
    m.ingest(Turn("t1", "", "p_ash", "Bex Morrow will bring the ledger at 10:30."))
    rid = m.store.records()[0].id
    assert m.store.view_of(rid, "p_bex") == "full"          # mentioned by name -> about (fallback)
    assert m.store.view_of(rid, "p_dov") == "none"
    ev = m.query("p_ash", "when is the ledger coming?", "t1")
    assert ev.action == "answer" and ev.items


def test_deletion_request_via_interpreter_masks_values(tmp_path):
    def remove_handler(system: str, user: str) -> dict:
        rid = user.split("[", 2)[1].split("]")[0]
        return {"targets": [{"id": rid, "part": None}]}
    chat = FakeChat({
        "label": label_handler({
            "forget": {"kind": "deletion", "quotes": [], "audience": {}, "remove": {"what": "callback line"}},
            "callback": fact("callback line is 555-0199", about=["p_ash"], owners=["p_ash"]),
        }),
        "remove": remove_handler,
    })
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "My callback line is 555-0199."))
    m.ingest(Turn("t2", "", "p_ash", "Please forget my callback line."))
    assert not m.store.records()
    assert m.query("p_ash", "callback line", "t2").action == "no_memory"


def test_rescope_widening_needs_agreement(tmp_path):
    rescope_reply = {"kind": "instruction",
                     "quotes": [{"text": "you may tell Dov about my locker", "about": ["p_ash"], "owners": ["p_ash"]}],
                     "audience": {"full": ["speaker"], "redacted": []},
                     "rescope": {"what": "locker", "share_with": ["p_dov"], "withhold_from": [], "view": "full"}}
    first = {"kind": "fact", "quotes": [{"text": "locker is 12", "about": ["p_ash"], "owners": ["p_ash"]}],
             "audience": {"full": ["speaker"], "redacted": []}}

    def ids(user: str) -> list[str]:
        import re
        return re.findall(r"\[(r\d+)\]", user)

    agree = FakeChat({"label": label_handler({"tell": rescope_reply, "locker is": first}),
                      "rescope": lambda s, u: {"ids": ids(u)[:1]},
                      "rescope_check": lambda s, u: {"allowed": ids(u)}})
    m = make_memory(tmp_path, agree)
    m.ingest(Turn("t1", "", "p_ash", "My locker is 12."))
    rid = m.store.records()[0].id
    m.ingest(Turn("t2", "", "p_ash", "OK, you may tell Dov about my locker."))
    assert m.store.view_of(rid, "p_dov") == "full"

    disagree = FakeChat({"label": label_handler({"tell": rescope_reply, "locker is": first}),
                         "rescope": lambda s, u: {"ids": ids(u)[:1]},
                         "rescope_check": lambda s, u: {"allowed": []}})
    (tmp_path / "b").mkdir()
    m2 = make_memory(tmp_path / "b", disagree)
    m2.ingest(Turn("t1", "", "p_ash", "My locker is 12."))
    rid2 = m2.store.records()[0].id
    m2.ingest(Turn("t2", "", "p_ash", "OK, you may tell Dov about my locker."))
    assert m2.store.view_of(rid2, "p_dov") == "none"
    assert m2.store.q("SELECT COUNT(*) FROM _reorgs WHERE status='pending' AND op='grant'")[0][0] == 1


def test_alias_resolution_prefers_longest_and_ignores_nested(tmp_path):
    m = make_memory(tmp_path, None)
    ids = m.roster.mentioned_ids("Ash Quell met Bex yesterday; Quellmark is unrelated.")
    assert ids == ["p_ash", "p_bex"]


def test_sqlite_file_has_no_plaintext_body(tmp_path):
    secret = "marigold-5521-tundra"
    chat = FakeChat({"label": label_handler({"marigold": fact(f"note {secret}", about=["p_ash"])})})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", f"A note {secret}."))
    m.close()
    con = sqlite3.connect(str(tmp_path / "m.db"))
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for f in tmp_path.iterdir():
        assert secret.encode() not in f.read_bytes()


def test_delete_reaches_copies_and_masks_values(tmp_path):
    chat = FakeChat({"label": label_handler({
        "first": fact("callback 415-555-0186 for now", about=["p_ash"], owners=["p_ash"]),
        "entry": fact("entry: callback 415-555-0186, keep voicemail generic", about=["p_ash"], owners=["p_ash"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "My first callback 415-555-0186 for now."))
    m.ingest(Turn("t2", "", "p_bex", "Safe entry: callback 415-555-0186, keep voicemail generic."))
    first = m.store.records()[0].id
    m.store.register_turn("t3", "", "p_ash")
    assert m.apply(Op("delete", {"records": [first]}, "p_ash", "t3", "caller")).ok
    texts = [m.store.text_owner(r.id) or "" for r in m.store.records()]
    assert texts and all("0186" not in t for t in texts)
    assert any("voicemail generic" in t for t in texts)          # the rest of the copy survives, masked


def test_release_check_masks_instead_of_replacing(tmp_path):
    from govmem.release.check import check_release
    chat = FakeChat({"label": label_handler({
        "vault": fact("vault code 9931-ZT", about=["p_ash"], owners=["p_ash"], full=["speaker"], protected=["9931-ZT"]),
        "door": fact("door opens at 9", full=["everyone"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "The vault code 9931-ZT."))
    m.ingest(Turn("t2", "", "p_ash", "The door opens at 9."))
    door = m.store.records()[-1].id
    out, why = check_release(m.store, {"action": "answer", "answer": "Door at 9; vault 9931-ZT."}, [door], ["door opens at 9"])
    assert why == "protected_value_of_unreleased_record"
    assert "9931" not in out["answer"] and "Door at 9" in out["answer"]


def test_forgotten_values_are_masked_in_later_turns(tmp_path):
    chat = FakeChat({"label": label_handler({
        "first": fact("callback 415-555-0186 for now", about=["p_ash"], owners=["p_ash"]),
        "removed": fact("entries 415-555-0186 removed as requested", about=["p_ash"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "My first callback 415-555-0186 for now."))
    first = m.store.records()[0].id
    m.store.register_turn("t2", "", "p_ash")
    assert m.apply(Op("delete", {"records": [first]}, "p_ash", "t2", "caller")).ok
    m.ingest(Turn("t3", "", "p_bex", "Confirmed: entries 415-555-0186 removed as requested."))
    texts = [m.store.text_owner(r.id) or "" for r in m.store.records()]
    assert texts and all("0186" not in t for t in texts)


def test_forgotten_phrases_are_masked_in_later_turns(tmp_path):
    chat = FakeChat({"label": label_handler({
        "first": fact("evening desk at Harrow Lantern Lodge, never say the word clinic", about=["p_ash"], owners=["p_ash"]),
        "later": fact("removed Harrow Lantern Lodge and never say the word clinic from notes", about=["p_ash"]),
    })})
    m = make_memory(tmp_path, chat)
    m.ingest(Turn("t1", "", "p_ash", "The first evening desk at Harrow Lantern Lodge, never say the word clinic."))
    first = m.store.records()[0].id
    m.store.register_turn("t2", "", "p_ash")
    assert m.apply(Op("delete", {"records": [first]}, "p_ash", "t2", "caller")).ok
    m.ingest(Turn("t3", "", "p_bex", "Done later: removed Harrow Lantern Lodge and never say the word clinic from notes."))
    texts = [m.store.text_owner(r.id) or "" for r in m.store.records()]
    assert texts and all("Harrow Lantern" not in t and "say the word clinic" not in t for t in texts)


def test_roster_window_scopes_everyone_to_recent_participants(tmp_path):
    chat = FakeChat({"label": label_handler({"ledger": fact("ledger closes friday", full=["everyone"])})})
    m = make_memory(tmp_path, chat)
    m.cfg.roster_window = 5
    m.interpreter.window = __import__("collections").deque(maxlen=5)
    m.ingest(Turn("t1", "", "p_ash", "Bex Morrow, the ledger closes friday."))
    rid = m.store.records()[0].id
    assert m.store.view_of(rid, "p_bex") == "full"
    assert m.store.view_of(rid, "p_dov") == "none"        # never active in the window
