"""The frozen envelope and its closed DDL: what a structure op can and cannot
do to a store."""

from __future__ import annotations

import sqlite3

import pytest

from memory_system import envelope, keyring
from memory_system.envelope import SchemaError
from memory_system.ops import ENVELOPE_COLUMNS


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = envelope.connect(":memory:")
    envelope.ensure_schema(c)
    return c


def put(conn: sqlite3.Connection, text: str, audience=("p1",), **kw) -> str:
    rid, _ = envelope.insert_record(
        conn,
        text=text,
        speaker_id=kw.pop("speaker", "p1"),
        speaker_role="r",
        about=kw.pop("about", ["e1"]),
        source_turn=kw.pop("turn", "t1"),
        created_at=kw.pop("at", "2026-01-01T09:00"),
        audience=audience,
        **kw,
    )
    return rid


def test_default_collection_carries_the_envelope(conn) -> None:
    assert envelope.collections(conn) == [envelope.DEFAULT_COLLECTION]
    assert envelope.columns(conn, envelope.DEFAULT_COLLECTION)[: len(ENVELOPE_COLUMNS)] == list(
        ENVELOPE_COLUMNS
    )
    envelope.ensure_schema(conn)  # idempotent
    assert envelope.collections(conn) == [envelope.DEFAULT_COLLECTION]


def test_insert_seals_and_grants(conn) -> None:
    rid = put(conn, "the code is zirconia-55", audience=("p1", "p2", keyring.OWNER))
    body = conn.execute("SELECT body FROM records WHERE record_id = ?", (rid,)).fetchone()[0]
    assert b"zirconia" not in bytes(body)
    assert envelope.audience(conn, rid) == ["p1", "p2"]
    assert keyring.open_for(conn, rid, "p2", body) == "the code is zirconia-55"
    assert keyring.open_for(conn, rid, "p3", body) is None
    assert envelope.record_collection(conn, rid) == "records"


def test_record_ids_are_deterministic_per_store() -> None:
    ids = []
    for _ in range(2):
        c = envelope.connect(":memory:")
        envelope.ensure_schema(c)
        ids.append([put(c, f"x{i}") for i in range(3)])
    assert ids[0] == ids[1] and len(set(ids[0])) == 3


def test_unmatched_field_values_are_returned_not_stored(conn) -> None:
    _, unmatched = envelope.insert_record(
        conn,
        text="x",
        speaker_id="p1",
        speaker_role="r",
        about=[],
        source_turn="t",
        created_at="a",
        audience=["p1"],
        field_values={"status": "open", "about": "hijack"},
    )
    assert unmatched == {"status": "open"}  # an envelope name is silently not a field


def test_add_field_index_rekey(conn) -> None:
    rid = put(conn, "x")
    envelope.add_field(conn, "records", "status", "TEXT", at="a")
    assert envelope.fields(conn, "records") == {"status": "TEXT"}
    assert envelope.rekey(conn, "records", "status", {rid: "open", "r_missing": "x"}) == [rid]
    assert envelope.get_record(conn, rid)["status"] == "open"
    name = envelope.add_index(conn, "records", ["status", "created_at"])
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
    with pytest.raises(SchemaError):
        envelope.add_field(conn, "records", "status", "TEXT", at="a")
    with pytest.raises(SchemaError):
        envelope.add_index(conn, "records", ["nope"])


@pytest.mark.parametrize("column", ENVELOPE_COLUMNS)
def test_no_ddl_can_touch_an_envelope_column(conn, column: str) -> None:
    before = envelope.columns(conn, "records")
    with pytest.raises(SchemaError):
        envelope.add_field(conn, "records", column, "TEXT", at="a")
    with pytest.raises(SchemaError):
        envelope.rekey(conn, "records", column, {})
    with pytest.raises(SchemaError):
        envelope.create_collection(conn, "things", [{"name": column, "type": "TEXT"}], at="a")
    assert envelope.columns(conn, "records") == before


@pytest.mark.parametrize("name", ["records", "_terms", "record_keys", "trace", "sqlite_master", "Bad"])
def test_collections_cannot_shadow_existing_or_scaffold_tables(conn, name: str) -> None:
    with pytest.raises(SchemaError):
        envelope.create_collection(conn, name, [], at="a")


def test_new_collection_has_envelope_then_fields(conn) -> None:
    envelope.create_collection(conn, "events", [{"name": "when_text", "type": "TEXT"}], at="a")
    cols = envelope.columns(conn, "events")
    assert cols[: len(ENVELOPE_COLUMNS)] == list(ENVELOPE_COLUMNS) and cols[-1] == "when_text"
    rid, _ = envelope.insert_record(
        conn,
        text="x",
        speaker_id="p",
        speaker_role="r",
        about=[],
        source_turn="t",
        created_at="a",
        audience=["p"],
        collection="events",
        field_values={"when_text": "noon"},
    )
    assert envelope.get_record(conn, rid)["when_text"] == "noon"


def test_views_always_filter_tombstones(conn) -> None:
    envelope.add_field(conn, "records", "status", "TEXT", at="a")
    keep, gone = put(conn, "keep"), put(conn, "gone")
    envelope.rekey(conn, "records", "status", {keep: "open", gone: "open"})
    sql = envelope.create_view(conn, "open_items", "records", [["status", "=", "open"]], "created_at", at="a")
    assert "tombstoned_at IS NULL" in sql
    stored = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'open_items'").fetchone()[0]
    assert "tombstoned_at IS NULL" in stored
    envelope.tombstone(conn, gone, "b")
    assert [r[0] for r in conn.execute("SELECT record_id FROM open_items")] == [keep]
    # even a predicate that asks for deleted rows cannot get them
    envelope.create_view(conn, "dead", "records", [["tombstoned_at", "IS NOT NULL"]], at="a")
    assert conn.execute("SELECT COUNT(*) FROM dead").fetchone()[0] == 0


def test_view_literals_are_quoted_not_spliced(conn) -> None:
    envelope.add_field(conn, "records", "status", "TEXT", at="a")
    envelope.create_view(conn, "odd", "records", [["status", "=", "x'); DROP TABLE records; --"]], at="a")
    assert "records" in envelope.collections(conn)
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_supersede_keeps_history(conn) -> None:
    old, new = put(conn, "v1"), put(conn, "v2", at="2026-01-02")
    envelope.supersede(conn, old, new, "2026-01-02")
    rec = envelope.get_record(conn, old)
    assert rec["valid_to"] == "2026-01-02" and rec["superseded_by"] == new
    assert envelope.audience(conn, old) == ["p1"]  # the old version is history, not deleted
    assert set(envelope.live_ids(conn)) == {new}
    assert set(envelope.live_ids(conn, current_only=False)) == {old, new}


def test_tombstone_destroys_keys_fields_and_index(conn) -> None:
    envelope.add_field(conn, "records", "status", "TEXT", at="a")
    rid = put(conn, "secret thing", audience=("p1", "p2"))
    envelope.rekey(conn, "records", "status", {rid: "secret-value"})
    body = envelope.get_record(conn, rid)["body"]
    assert envelope.tombstone(conn, rid, "b", null_body=True)
    rec = envelope.get_record(conn, rid)
    assert rec["tombstoned_at"] == "b" and rec["body"] is None and rec.get("status") is None
    assert keyring.holders(conn, rid) == []
    assert keyring.open_for(conn, rid, keyring.OWNER, body) is None
    assert conn.execute("SELECT COUNT(*) FROM _terms WHERE record_id = ?", (rid,)).fetchone()[0] == 0
    assert not keyring.grant(conn, rid, "p1")


def test_derived_audience_is_the_intersection(conn) -> None:
    a = put(conn, "a", audience=("p1", "p2", "p3"))
    b = put(conn, "b", audience=("p2", "p3"))
    conn.execute("INSERT INTO _restrictions VALUES (?, 'p1', 'summary', 'x')", (b,))
    assert envelope.derived_audience(conn, [a, b]) == {"p1", "p2", "p3"}
    keyring.revoke(conn, b, "p3")
    assert envelope.derived_audience(conn, [a, b]) == {"p1", "p2"}
    d = put(conn, "summary", audience=("p1",), derived_from=[a, b], granularity="summary")
    assert envelope.derived_of(conn, a) == [d]


def test_schema_summary_shape(conn) -> None:
    envelope.add_field(conn, "records", "status", "INTEGER", at="a")
    envelope.create_view(conn, "v_open", "records", [["status", "=", 1]], at="a")
    put(conn, "x")
    s = envelope.schema_summary(conn)
    assert s["collections"]["records"] == {"fields": {"status": "INTEGER"}, "records": 1}
    assert s["views"]["v_open"]["collection"] == "records"
