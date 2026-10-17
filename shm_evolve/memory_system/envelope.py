"""The frozen envelope, and the only code that writes DDL.

Every collection is a table whose first columns are the envelope (DESIGN.md
§2.4); whatever fields the librarian adds sit after them. The model never
writes SQL: structure ops arrive as validated identifiers and predicate
triples (ops.py) and are turned into statements here, so the set of shapes a
statement can take is closed. Anything that names an envelope column -- or a
table the scaffold owns -- is refused with a reason the trace records.

Side tables the scaffold owns are prefixed with `_`. `ops.is_identifier`
requires a leading letter, so no model-chosen name can ever collide with one
or address one as a collection.

Audience is not a column. It is who holds a wrap in `keyring.record_keys`,
read by `audience()`. A record's body is sealed at insert and the audience is
granted at insert; tombstoning destroys every wrap (`keyring.shred`) and nulls
every non-envelope field, because "deleted" here means unrecoverable, not
hidden. `PRAGMA secure_delete` makes SQLite zero the freed bytes, so the
plaintext of an added field does not survive in a free page either.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Iterable
from typing import Any

from memory_system import keyring, terms
from memory_system.ops import (
    ENVELOPE_COLUMNS,
    FIELD_TYPES,
    GRANULARITIES,
    Clause,
    OpError,
    is_identifier,
    parse_predicate,
)

DEFAULT_COLLECTION = "records"

_ENVELOPE_DDL = """
    record_id     TEXT PRIMARY KEY,
    collection    TEXT NOT NULL,
    body          BLOB,
    speaker_id    TEXT NOT NULL,
    speaker_role  TEXT NOT NULL,
    about         TEXT NOT NULL,
    source_turn   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    superseded_by TEXT,
    derived_from  TEXT,
    granularity   TEXT NOT NULL,
    tombstoned_at TEXT"""

_SIDE_TABLES = """
CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value BLOB);
CREATE TABLE IF NOT EXISTS _collections (name TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _fields (
    collection TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (collection, name));
CREATE TABLE IF NOT EXISTS _views (
    name TEXT PRIMARY KEY, collection TEXT NOT NULL, where_json TEXT NOT NULL, order_by TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _record_index (record_id TEXT PRIMARY KEY, collection TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _terms (
    record_id TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY (record_id, term));
CREATE INDEX IF NOT EXISTS _terms_by_term ON _terms(term);
CREATE TABLE IF NOT EXISTS _links (
    record_id TEXT NOT NULL, other_record_id TEXT NOT NULL, relation TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _restrictions (
    record_id TEXT NOT NULL, principal_id TEXT NOT NULL, granularity TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (record_id, principal_id));
CREATE TABLE IF NOT EXISTS _policy (
    fact_id TEXT PRIMARY KEY, links TEXT NOT NULL, holders TEXT NOT NULL, qualified INTEGER NOT NULL,
    attributes TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, valid_to TEXT);
CREATE TABLE IF NOT EXISTS _ledger (
    principal_id TEXT NOT NULL, record_id TEXT, scope TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS _ledger_by_principal ON _ledger(principal_id);
CREATE TABLE IF NOT EXISTS _expiries (
    record_id TEXT NOT NULL, principal_id TEXT NOT NULL, at TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0);
"""


class SchemaError(OpError):
    """A structure op the envelope refuses. The message is the trace reason."""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA secure_delete = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    keyring.ensure_schema(conn)
    conn.executescript(_SIDE_TABLES)
    if not conn.execute("SELECT 1 FROM _meta WHERE key = 'index_key'").fetchone():
        conn.execute("INSERT INTO _meta(key, value) VALUES ('index_key', ?)", (os.urandom(32),))
    if DEFAULT_COLLECTION not in collections(conn):
        create_collection(conn, DEFAULT_COLLECTION, [], at="")


def index_key(conn: sqlite3.Connection) -> bytes:
    return bytes(conn.execute("SELECT value FROM _meta WHERE key = 'index_key'").fetchone()[0])


def new_record_id(conn: sqlite3.Connection) -> str:
    """A per-store counter, so replaying an episode yields the same ids (the
    judge compares runs by record id) and ids sort in creation order."""
    row = conn.execute("SELECT value FROM _meta WHERE key = 'next_record'").fetchone()
    n = int(row[0]) if row else 1
    conn.execute("INSERT OR REPLACE INTO _meta(key, value) VALUES ('next_record', ?)", (str(n + 1),))
    return f"r{n:05d}"


# ---------------------------------------------------------------- catalogue


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def collections(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM _collections ORDER BY created_at, name")]


def views(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        r[0]: {"collection": r[1], "where": json.loads(r[2]), "order_by": r[3]}
        for r in conn.execute("SELECT name, collection, where_json, order_by FROM _views ORDER BY name")
    }


def columns(conn: sqlite3.Connection, collection: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{collection}")')]


def fields(conn: sqlite3.Connection, collection: str) -> dict[str, str]:
    """The non-envelope columns of a collection, name -> type."""
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT name, type FROM _fields WHERE collection = ? ORDER BY created_at, name", (collection,)
        )
    }


def schema_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the prompts are shown of the current structure."""
    out: dict[str, Any] = {"collections": {}, "views": views(conn)}
    for c in collections(conn):
        n = conn.execute(f'SELECT COUNT(*) FROM "{c}" WHERE tombstoned_at IS NULL').fetchone()[0]
        out["collections"][c] = {"fields": fields(conn, c), "records": n}
    return out


def _require_collection(conn: sqlite3.Connection, collection: Any) -> str:
    if not isinstance(collection, str) or collection not in collections(conn):
        raise SchemaError(f"no collection {collection!r}")
    return collection


def _require_new_name(conn: sqlite3.Connection, name: Any, what: str) -> str:
    if not is_identifier(name):
        raise SchemaError(f"{what}: {name!r} is not an allowed identifier (envelope or scaffold name?)")
    if _table_exists(conn, name):
        raise SchemaError(f"{what}: {name!r} already exists")
    return name


def _require_field_name(name: Any) -> str:
    if not isinstance(name, str) or name in ENVELOPE_COLUMNS or not is_identifier(name):
        raise SchemaError(f"field {name!r} names the envelope or is not an identifier")
    return name


# ---------------------------------------------------------------- DDL (the closed vocabulary)


def create_collection(
    conn: sqlite3.Connection, name: str, field_specs: list[dict[str, str]], *, at: str
) -> None:
    _require_new_name(conn, name, "add_collection")
    extra: list[str] = []
    seen: set[str] = set()
    for spec in field_specs:
        fname, ftype = _require_field_name(spec.get("name")), spec.get("type")
        if ftype not in FIELD_TYPES:
            raise SchemaError(f"add_collection: type {ftype!r} not in {FIELD_TYPES}")
        if fname in seen:
            raise SchemaError(f"add_collection: duplicate field {fname!r}")
        seen.add(fname)
        extra.append(f'"{fname}" {ftype}')
    cols = _ENVELOPE_DDL + "".join(f",\n    {e}" for e in extra)
    conn.execute(f'CREATE TABLE "{name}" ({cols})')
    conn.execute(f'CREATE INDEX "{name}__created" ON "{name}"(created_at)')
    conn.execute("INSERT INTO _collections(name, created_at) VALUES (?, ?)", (name, at))
    for spec in field_specs:
        conn.execute(
            "INSERT INTO _fields(collection, name, type, created_at) VALUES (?, ?, ?, ?)",
            (name, spec["name"], spec["type"], at),
        )


def add_field(conn: sqlite3.Connection, collection: str, name: str, ftype: str, *, at: str) -> None:
    _require_collection(conn, collection)
    _require_field_name(name)
    if ftype not in FIELD_TYPES:
        raise SchemaError(f"add_field: type {ftype!r} not in {FIELD_TYPES}")
    if name in columns(conn, collection):
        raise SchemaError(f"add_field: {collection}.{name} already exists")
    conn.execute(f'ALTER TABLE "{collection}" ADD COLUMN "{name}" {ftype}')
    conn.execute(
        "INSERT INTO _fields(collection, name, type, created_at) VALUES (?, ?, ?, ?)",
        (collection, name, ftype, at),
    )


def add_index(conn: sqlite3.Connection, collection: str, field_names: list[str]) -> str:
    _require_collection(conn, collection)
    cols = columns(conn, collection)
    if not field_names or len(field_names) > 4:
        raise SchemaError("add_index: 1 to 4 fields")
    for f in field_names:
        if f not in cols or f == "body":
            raise SchemaError(f"add_index: {collection} has no indexable field {f!r}")
    name = f"ix_{collection}_{'_'.join(field_names)}"[:60]
    joined = ", ".join(f'"{f}"' for f in field_names)
    conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{collection}"({joined})')
    return name


def _literal(value: Any) -> str:
    """A SQL literal for a view definition (views cannot bind parameters)."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError("non-finite literal")
        return repr(value)
    if isinstance(value, str):
        if "\x00" in value or len(value) > 200:
            raise SchemaError("string literal too long or contains NUL")
        return "'" + value.replace("'", "''") + "'"
    raise SchemaError(f"unsupported literal {value!r}")


def _clause_sql(clauses: list[Clause], allowed: list[str]) -> str:
    parts: list[str] = []
    for c in clauses:
        if c.field not in allowed or c.field == "body":
            raise SchemaError(f"predicate field {c.field!r} not in the collection")
        if c.op.startswith("IS"):
            parts.append(f'"{c.field}" {c.op}')
        else:
            parts.append(f'"{c.field}" {c.op} {_literal(c.value)}')
    return " AND ".join(parts)


def create_view(
    conn: sqlite3.Connection, name: str, collection: str, where: Any, order_by: str | None = None, *, at: str
) -> str:
    """CREATE VIEW, always AND-ed with the tombstone filter -- a view can never
    resurrect a deleted record, whatever predicate the model asked for."""
    _require_new_name(conn, name, "create_view")
    _require_collection(conn, collection)
    cols = columns(conn, collection)
    clauses = parse_predicate(where)
    pred = _clause_sql(clauses, cols)
    sql = f'CREATE VIEW "{name}" AS SELECT * FROM "{collection}" WHERE tombstoned_at IS NULL'
    if pred:
        sql += f" AND ({pred})"
    if order_by is not None:
        if order_by not in cols or order_by == "body":
            raise SchemaError(f"create_view: order_by {order_by!r} not in the collection")
        sql += f' ORDER BY "{order_by}"'
    conn.execute(sql)
    conn.execute(
        "INSERT INTO _views(name, collection, where_json, order_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, collection, json.dumps(where or []), order_by, at),
    )
    return sql


def rekey(conn: sqlite3.Connection, collection: str, field: str, assignments: dict[str, Any]) -> list[str]:
    """Fill `field` on existing live records. The field must already exist
    (the librarian adds it first if needed). Returns the record ids changed."""
    _require_collection(conn, collection)
    _require_field_name(field)
    if field not in columns(conn, collection):
        raise SchemaError(f"rekey: {collection}.{field} does not exist")
    changed: list[str] = []
    for rid, value in assignments.items():
        if value is not None and not isinstance(value, (str, int, float)):
            continue
        cur = conn.execute(
            f'UPDATE "{collection}" SET "{field}" = ? WHERE record_id = ? AND tombstoned_at IS NULL',
            (value, rid),
        )
        if cur.rowcount:
            changed.append(rid)
    return changed


# ---------------------------------------------------------------- records


def record_collection(conn: sqlite3.Connection, record_id: str) -> str | None:
    row = conn.execute("SELECT collection FROM _record_index WHERE record_id = ?", (record_id,)).fetchone()
    return row[0] if row else None


def get_record(conn: sqlite3.Connection, record_id: str) -> dict[str, Any] | None:
    """The envelope and fields of one record. The body stays sealed."""
    coll = record_collection(conn, record_id)
    if coll is None:
        return None
    cur = conn.execute(f'SELECT * FROM "{coll}" WHERE record_id = ?', (record_id,))
    row = cur.fetchone()
    if row is None:
        return None
    rec = dict(zip([d[0] for d in cur.description], row, strict=True))
    rec["about"] = json.loads(rec["about"] or "[]")
    rec["derived_from"] = json.loads(rec["derived_from"] or "[]")
    return rec


def is_live(rec: dict[str, Any] | None) -> bool:
    return rec is not None and rec.get("tombstoned_at") is None


def record_fields(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rec.items() if k not in ENVELOPE_COLUMNS and v is not None}


def insert_record(
    conn: sqlite3.Connection,
    *,
    text: str,
    speaker_id: str,
    speaker_role: str,
    about: Iterable[str],
    source_turn: str,
    created_at: str,
    audience: Iterable[str],
    collection: str = DEFAULT_COLLECTION,
    granularity: str = "full",
    derived_from: Iterable[str] = (),
    field_values: dict[str, Any] | None = None,
    record_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Seal, store, grant. Returns (record_id, the field values that had no
    column to land in) -- the latter is the librarian's schema_mismatch."""
    _require_collection(conn, collection)
    if granularity not in GRANULARITIES:
        raise SchemaError(f"granularity {granularity!r}")
    rid = record_id or new_record_id(conn)
    about_list = list(dict.fromkeys(a for a in about if isinstance(a, str) and a))
    body = keyring.seal(conn, rid, text)
    cols = columns(conn, collection)
    landed: dict[str, Any] = {}
    unmatched: dict[str, Any] = {}
    for k, v in (field_values or {}).items():
        if not isinstance(k, str) or k in ENVELOPE_COLUMNS:
            continue
        if k in cols and isinstance(v, (str, int, float)):
            landed[k] = v
        else:
            unmatched[k] = v
    names = [
        "record_id",
        "collection",
        "body",
        "speaker_id",
        "speaker_role",
        "about",
        "source_turn",
        "created_at",
        "valid_from",
        "derived_from",
        "granularity",
        *landed,
    ]
    values = [
        rid,
        collection,
        body,
        speaker_id,
        speaker_role,
        json.dumps(about_list),
        source_turn,
        created_at,
        created_at,
        json.dumps(list(derived_from)),
        granularity,
        *landed.values(),
    ]
    quoted = ", ".join(f'"{n}"' for n in names)
    conn.execute(f'INSERT INTO "{collection}" ({quoted}) VALUES ({", ".join("?" * len(names))})', values)
    conn.execute("INSERT INTO _record_index(record_id, collection) VALUES (?, ?)", (rid, collection))
    key = index_key(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO _terms(record_id, term) VALUES (?, ?)",
        [(rid, h) for h in terms.blind_all(key, terms.terms(text) | {a.lower() for a in about_list})],
    )
    for pid in audience:
        if pid and pid != keyring.OWNER:
            keyring.grant(conn, rid, pid)
    return rid, unmatched


def supersede(conn: sqlite3.Connection, old_id: str, new_id: str, at: str) -> None:
    coll = record_collection(conn, old_id)
    if coll is None:
        raise SchemaError(f"no record {old_id}")
    conn.execute(
        f'UPDATE "{coll}" SET valid_to = ?, superseded_by = ? WHERE record_id = ? AND valid_to IS NULL',
        (at, new_id, old_id),
    )


def tombstone(conn: sqlite3.Connection, record_id: str, at: str, *, null_body: bool = False) -> bool:
    """Delete: mark, destroy every wrap, null every added field, drop the
    record's index terms. With `null_body` the ciphertext goes too (shred)."""
    coll = record_collection(conn, record_id)
    if coll is None:
        return False
    extra = [c for c in columns(conn, coll) if c not in ENVELOPE_COLUMNS]
    sets = ["tombstoned_at = COALESCE(tombstoned_at, ?)"] + [f'"{c}" = NULL' for c in extra]
    if null_body:
        sets.append("body = NULL")
    conn.execute(f'UPDATE "{coll}" SET {", ".join(sets)} WHERE record_id = ?', (at, record_id))
    keyring.shred(conn, record_id)
    conn.execute("DELETE FROM _terms WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM _restrictions WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM _expiries WHERE record_id = ?", (record_id,))
    return True


def audience(conn: sqlite3.Connection, record_id: str) -> list[str]:
    return [p for p in keyring.holders(conn, record_id) if p != keyring.OWNER]


def restricted_to(conn: sqlite3.Connection, record_id: str, granularity: str | None = None) -> list[str]:
    sql = "SELECT principal_id FROM _restrictions WHERE record_id = ?"
    params: list[Any] = [record_id]
    if granularity:
        sql += " AND granularity = ?"
        params.append(granularity)
    return [r[0] for r in conn.execute(sql, params)]


def derived_audience(conn: sqlite3.Connection, source_ids: Iterable[str]) -> set[str]:
    """Intersection of the sources' audiences. A principal restricted to a
    summary of a source counts as holding it here: a summary is exactly what
    they may still see."""
    result: set[str] | None = None
    for sid in source_ids:
        held = set(audience(conn, sid)) | set(restricted_to(conn, sid, "summary"))
        result = held if result is None else result & held
    return result or set()


def derived_of(conn: sqlite3.Connection, record_id: str) -> list[str]:
    """Live records that list `record_id` among their sources."""
    needle = json.dumps(record_id)
    out: list[str] = []
    for c in collections(conn):
        out += [
            r[0]
            for r in conn.execute(
                f'SELECT record_id FROM "{c}" WHERE tombstoned_at IS NULL AND instr(derived_from, ?) > 0',
                (needle,),
            )
        ]
    return out


def live_ids(
    conn: sqlite3.Connection, *, about_any: Iterable[str] = (), current_only: bool = True
) -> list[str]:
    """Live record ids, optionally only those about any of the given ids."""
    about_list = [json.dumps(a) for a in about_any]
    out: list[str] = []
    for c in collections(conn):
        sql = f'SELECT record_id FROM "{c}" WHERE tombstoned_at IS NULL'
        params: list[Any] = []
        if current_only:
            sql += " AND valid_to IS NULL"
        if about_list:
            sql += " AND (" + " OR ".join("instr(about, ?) > 0" for _ in about_list) + ")"
            params += about_list
        out += [r[0] for r in conn.execute(sql, params)]
    return out
