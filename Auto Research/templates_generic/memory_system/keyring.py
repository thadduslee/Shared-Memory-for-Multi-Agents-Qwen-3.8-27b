"""Per-principal keys: the one part of this memory that the loop may not edit.

WHAT THIS GUARANTEES, AND WHAT IT DOES NOT
------------------------------------------
Every record body is sealed under its own data key. That key is never stored in
the clear: it exists only WRAPPED, once per principal the access policy admits,
plus once for the store's own maintenance identity. `open()` is the only way to
a plaintext, and it needs a wrap for the requester who is asking. So no edit to
retrieval, ranking, widening or presentation can hand the answerer a record the
policy did not grant -- the ciphertext is all there is.

WHO IS ADMITTED is decided by `grant_by_policy()` below, from two tables the
loop is free to edit -- `role_grants` (which roles read which tier, and whether
a named relationship is required) and `relationships` (who holds what over
which object, imported from the episode). The RULE that combines them is
frozen with the cipher:

  * anyone the record is ABOUT (its subject or object) holds it. The AUTHOR
    does not, by that fact alone: a requester's own earlier turns are their
    questions and claims, not the memory's knowledge, and handing them back
    is how a family member restricted to scheduling got "I can confirm at a
    high level that the procedure was carried out" -- assembled from her own
    questions. Authors reach their own records through the same policy as
    anyone else (a patient is the record's object; staff through their tier);
  * a principal who is BOUND to objects in this episode (holds any declared
    relationship) is confined to them: a key only for records about an object
    they are related to, and only through a tier their role is granted --
    UNLESS the relationship carries a SCOPE ("rides_and_check_in_only",
    "summary_only", ...). A scoped relationship grants nothing by tier: it
    unlocks exactly the record kinds a `scope_grants` row names for it, and
    in the mode that row gives ('full', or 'summary': the body is withheld
    and only the record's existence and kind are released);
  * a principal who is UNBOUND (no declared relationship at all -- a nurse, a
    scheduler, an SRE) is governed by tier alone, and only through a grant
    that names no required relationship.

So a family member or a contractor can never hold a key to another patient's or
another project's records however the tables are edited, while staff roles with
no per-object binding are reachable through `role_grants`. What the answerer
SAYS once it legitimately holds a record is a leak class that lives past
decryption, and this file does not govern it.

WHY IT IS FROZEN. The Developer's tools refuse to write this file (see
nodes/dev_tools.py PROTECTED_FILES) and tests/test_keyring.py is a contract test.
Enforcement that can be edited by the thing it constrains is not enforcement.

The cipher is an HMAC-SHA256 counter keystream: no third-party dependency, and
destroying a key destroys the plaintext. It has no authentication tag.
# TODO(real): AES-GCM via `cryptography` before any clinical use.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3

#: The store's own identity. Holds a wrap for every record so the store can
#: index, match deletion requests and shred; never a requester.
OWNER = "__store__"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS principal_keys (
    principal_id  TEXT PRIMARY KEY,
    key_material  BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS record_keys (
    record_id     TEXT NOT NULL,
    principal_id  TEXT NOT NULL,
    wrapped       BLOB NOT NULL,
    granted_at    TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'full',   -- full | summary
    PRIMARY KEY (record_id, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_record_keys_principal ON record_keys(principal_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    principal_key(conn, OWNER)


def _scope_mode(conn: sqlite3.Connection, rel_type: str, scope: str, kind: str) -> str | None:
    """The mode a `scope_grants` row gives this (relationship, scope, kind), or None.

    For a SCOPED relationship (`scope` non-empty) a row is required: exact scope
    or '*'. For an UNSCOPED one (`scope == ''`) the tier default is 'full', and
    a row with scope '' for that relationship type overrides it -- the lever
    for "this relationship type only ever sees a summary" when the data
    declares no scope (an executive sponsor, say).
    """
    if scope:
        row = conn.execute(
            "SELECT mode FROM scope_grants WHERE rel_type = ? AND scope IN (?, '*') "
            "AND kind IN (?, '*') ORDER BY (scope = '*'), (kind = '*') LIMIT 1",
            (rel_type, scope, kind),
        ).fetchone()
        return str(row[0]) if row else None
    row = conn.execute(
        "SELECT mode FROM scope_grants WHERE rel_type = ? AND scope = '' "
        "AND kind IN (?, '*') ORDER BY (kind = '*') LIMIT 1",
        (rel_type, kind),
    ).fetchone()
    return str(row[0]) if row else "full"


def grant_by_policy(
    conn: sqlite3.Connection, record_id: str, subject_id: str, object_id: str,
    sensitivity: str, author_id: str, kind: str = "",
) -> int:
    """Wrap this record's key for every principal the rule above admits.

    Reads `principals`, `role_grants`, `relationships` and `scope_grants` as the
    store defines them. Returns the number of principals granted (the author
    included only if the policy admits them).
    """
    about = {s for s in (subject_id, object_id) if s}
    granted = 0
    for row in conn.execute("SELECT principal_id, role FROM principals").fetchall():
        pid, role = str(row[0]), str(row[1])
        if pid in about:
            grant(conn, record_id, pid)
            granted += 1
            continue
        tier = conn.execute(
            "SELECT requires_rel FROM role_grants WHERE role = ? AND sensitivity = ?",
            (role, sensitivity),
        ).fetchone()
        if tier is None:
            continue
        requires_rel = str(tier[0] or "")
        held = conn.execute(
            "SELECT rel_type, object_id, scope FROM relationships WHERE subject_id = ?", (pid,)
        ).fetchall()
        mode: str | None = None
        if not held:
            # UNBOUND: tier alone, and only a grant that asks for no relationship.
            mode = "full" if requires_rel == "" else None
        else:
            # BOUND: confined to the objects they hold a relationship over. An
            # unscoped relationship admits by tier; a scoped one admits only
            # what a scope_grants row unlocks, in that row's mode. 'full' wins
            # over 'summary' when several relationships apply.
            for rel_type, obj, scope in ((str(r[0]), str(r[1]), str(r[2] or "")) for r in held):
                if obj not in about or requires_rel not in ("", rel_type):
                    continue
                this = _scope_mode(conn, rel_type, scope, kind)
                if this == "full":
                    mode = "full"
                    break
                if this == "summary":
                    mode = "summary"
        if mode:
            grant(conn, record_id, pid, mode)
            granted += 1
    return granted


def _keystream(key: bytes, nonce: str, data: bytes) -> bytes:
    """XOR `data` with HMAC-SHA256(key, nonce || counter). Symmetric."""
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        out += hmac.new(key, f"{nonce}:{counter}".encode(), hashlib.sha256).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[: len(data)], strict=False))


def principal_key(conn: sqlite3.Connection, principal_id: str) -> bytes:
    """This principal's key, created on first use."""
    row = conn.execute(
        "SELECT key_material FROM principal_keys WHERE principal_id = ?", (principal_id,)
    ).fetchone()
    if row is not None:
        return bytes(row[0])
    key = os.urandom(32)
    conn.execute(
        "INSERT INTO principal_keys(principal_id, key_material) VALUES (?, ?)", (principal_id, key)
    )
    return key


def _wrap(
    conn: sqlite3.Connection, record_id: str, principal_id: str, data_key: bytes,
    mode: str = "full",
) -> None:
    pk = principal_key(conn, principal_id)
    conn.execute(
        "INSERT OR REPLACE INTO record_keys(record_id, principal_id, wrapped, granted_at, mode) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
        (record_id, principal_id, _keystream(pk, record_id, data_key), mode),
    )


def mode_for(conn: sqlite3.Connection, record_id: str, principal_id: str) -> str | None:
    """'full' or 'summary' if the principal holds a wrap, else None."""
    row = conn.execute(
        "SELECT mode FROM record_keys WHERE record_id = ? AND principal_id = ?",
        (record_id, principal_id),
    ).fetchone()
    return str(row[0]) if row else None


def _unwrap(conn: sqlite3.Connection, record_id: str, principal_id: str) -> bytes | None:
    row = conn.execute(
        "SELECT wrapped FROM record_keys WHERE record_id = ? AND principal_id = ?",
        (record_id, principal_id),
    ).fetchone()
    if row is None:
        return None
    return _keystream(principal_key(conn, principal_id), record_id, bytes(row[0]))


def seal(conn: sqlite3.Connection, record_id: str, plaintext: str) -> bytes:
    """Encrypt a body under a fresh data key, wrapped for OWNER only."""
    data_key = os.urandom(32)
    _wrap(conn, record_id, OWNER, data_key)
    return _keystream(data_key, record_id, plaintext.encode("utf-8"))


def grant(
    conn: sqlite3.Connection, record_id: str, principal_id: str, mode: str = "full"
) -> bool:
    """Wrap the record's data key for `principal_id`. False if the record is shredded."""
    data_key = _unwrap(conn, record_id, OWNER)
    if data_key is None:
        return False
    _wrap(conn, record_id, principal_id, data_key, mode)
    return True


def revoke(conn: sqlite3.Connection, record_id: str, principal_id: str) -> None:
    conn.execute(
        "DELETE FROM record_keys WHERE record_id = ? AND principal_id = ?", (record_id, principal_id)
    )


def open_for(
    conn: sqlite3.Connection, record_id: str, principal_id: str, ciphertext: bytes | None
) -> str | None:
    """The plaintext, if and only if `principal_id` holds a wrap for this record."""
    if not ciphertext:
        return None
    data_key = _unwrap(conn, record_id, principal_id)
    if data_key is None:
        return None
    return _keystream(data_key, record_id, bytes(ciphertext)).decode("utf-8", "replace")


def holders(conn: sqlite3.Connection, record_id: str) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT principal_id FROM record_keys WHERE record_id = ? ORDER BY principal_id",
            (record_id,),
        )
    ]


def shred(conn: sqlite3.Connection, record_id: str) -> None:
    """Destroy every wrap, OWNER's included. The ciphertext is then unrecoverable."""
    conn.execute("DELETE FROM record_keys WHERE record_id = ?", (record_id,))
