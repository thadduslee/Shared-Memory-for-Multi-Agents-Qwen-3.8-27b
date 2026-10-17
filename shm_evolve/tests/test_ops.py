"""The closed vocabularies and grammars in ops.py: what a model reply can and
cannot turn into."""

from __future__ import annotations

import pytest

from memory_system import ops
from memory_system.ops import (
    ENVELOPE_COLUMNS,
    Chunk,
    ChunkKind,
    Op,
    OpError,
    extract_json,
    is_identifier,
    parse_ops,
    parse_predicate,
    parse_search,
    predicate_sql,
    validate_op,
)


def test_every_op_has_required_args_and_a_group() -> None:
    assert set(ops.REQUIRED_ARGS) == set(ops.ALL_OPS)
    for name in ops.ALL_OPS:
        assert Op(name).group in {"record", "policy", "structure", "control"}
    assert Op("invented").group == "unknown"


def test_parse_ops_accepts_valid_and_reports_invalid() -> None:
    reply = """Sure:
```json
{"ops": [{"op": "add", "chunk_id": "t1#0"},
         {"op": "teleport", "chunk_id": "t1#0"},
         {"op": "grant", "record_ids": ["r1"], "principal_ids": ["p"]},
         "not an op"]}
```"""
    good, bad = parse_ops(reply)
    assert [o.name for o in good] == ["add"]
    assert len(bad) == 3
    assert any("teleport" in b for b in bad)
    assert any("authority" in b or "missing" in b for b in bad)


def test_parse_ops_never_raises_on_garbage() -> None:
    for junk in (None, "", "no json here", "[1, 2", 42, {"ops": "x"}):
        good, bad = parse_ops(junk)
        assert good == [] and bad


def test_default_chunk_id_and_signal_kind_propagate() -> None:
    good, bad = parse_ops(
        [{"op": "noop"}, {"op": "shred", "record_ids": ["r1"], "signal_kind": "instruction"}],
        default_chunk_id="t9#0",
    )
    # The default is provenance only: it does not stand in for a REQUIRED
    # chunk_id, so a bare noop is rejected (and the processor's coverage
    # rule then gives that chunk its fallback).
    assert [o.name for o in good] == ["shred"] and len(bad) == 1
    assert good[0].chunk_id == "t9#0" and good[0].signal_kind == "instruction"


def test_grant_requires_a_string_authority() -> None:
    with pytest.raises(OpError):
        validate_op(Op("grant", {"record_ids": ["r1"], "principal_ids": ["p"], "authority": None}))
    validate_op(Op("grant", {"record_ids": ["r1"], "principal_ids": ["p"], "authority": "q"}))


@pytest.mark.parametrize("column", ENVELOPE_COLUMNS)
def test_structure_ops_cannot_name_envelope_columns(column: str) -> None:
    assert not is_identifier(column)
    for op in (
        Op("add_field", {"collection": "records", "name": column, "type": "TEXT"}),
        Op("add_collection", {"name": "things", "fields": [{"name": column, "type": "TEXT"}]}),
        Op("rekey", {"collection": "records", "field": column, "assignments": {}}),
        Op("add_collection", {"name": column, "fields": []}),
    ):
        with pytest.raises(OpError):
            validate_op(op)


def test_identifiers_are_lowercase_and_bounded() -> None:
    assert is_identifier("due_date")
    for bad in ("Due", "1x", "_hidden", "a-b", "a" * 49, "x;drop", "", None, "trace", "record_keys"):
        assert not is_identifier(bad)


def test_field_types_are_closed() -> None:
    with pytest.raises(OpError):
        validate_op(Op("add_field", {"collection": "records", "name": "x", "type": "BLOB"}))


def test_predicate_grammar() -> None:
    clauses = parse_predicate([["status", "=", "open"], ["valid_to", "IS NULL"], ["n", ">=", 3]])
    sql, params = predicate_sql(clauses)
    assert sql == '"status" = ? AND "valid_to" IS NULL AND "n" >= ?'
    assert params == ["open", 3]
    for bad in (
        "status = 'x'",
        [["status", "OR", "x"]],
        [["x; drop", "=", 1]],
        [["s", "=", ["a"]]],
        [["s", "="]],
    ):
        with pytest.raises(OpError):
            parse_predicate(bad)


def test_create_view_validates_where_and_order_by() -> None:
    validate_op(
        Op(
            "create_view",
            {
                "name": "open_items",
                "collection": "records",
                "where": [["status", "=", "open"]],
                "order_by": "created_at",
            },
        )
    )
    with pytest.raises(OpError):
        validate_op(Op("create_view", {"name": "v", "collection": "records", "where": "1=1"}))
    with pytest.raises(OpError):
        validate_op(Op("create_view", {"name": "v", "collection": "records", "where": [], "order_by": "x y"}))


def test_search_spec() -> None:
    spec = parse_search(
        {
            "collections": ["records"],
            "terms": ["a", " ", 3],
            "about": ["p1"],
            "where": [["status", "=", "x"]],
            "current_only": False,
            "limit": 999,
        }
    )
    assert spec.terms == ["a"] and spec.about == ["p1"] and not spec.current_only and spec.limit == 40
    with pytest.raises(OpError):
        parse_search({"collections": ["Bad Name"]})
    with pytest.raises(OpError):
        parse_search(["not", "a", "dict"])


def test_extract_json_prefers_fenced_then_outermost() -> None:
    assert extract_json('x ```json\n{"a": 1}\n``` y') == {"a": 1}
    assert extract_json('noise [{"op": "noop"}, {"op": "noop"}] tail') == [{"op": "noop"}, {"op": "noop"}]
    assert extract_json("nothing") is None


def test_tightening_is_a_property_of_the_kind() -> None:
    def chunk(kind: str) -> Chunk:
        return Chunk("c", "t", "ts", "p", "r", "text", kind)

    assert {k for k in ChunkKind if chunk(k).is_tightening} == {"revoke", "restrict", "delete"}
