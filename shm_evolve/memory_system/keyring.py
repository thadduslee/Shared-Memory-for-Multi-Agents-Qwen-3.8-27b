"""Per-principal keys: the one part of this memory that the loop may not edit.

WHAT THIS GUARANTEES, AND WHAT IT DOES NOT
------------------------------------------
A body sealed with `seal()` is encrypted under its own data key. That key is
never stored in the clear: it exists only WRAPPED, once per principal that
`grant()` was called for, plus once for the store's own maintenance identity
(`OWNER`). `open_for()` is the only way to a plaintext, and it needs a wrap for
the principal who is asking. So whatever the system's retrieval, ranking or
presentation does, it cannot hand out a body the policy never granted -- the
ciphertext is all there is. `shred()` destroys every wrap: the plaintext is
then unrecoverable by any code path.

It does NOT decide who is granted. That -- at what granularity a body is
sealed, which principals get a wrap, when, and when it is revoked -- is the
memory system's policy and is the evolving part. A wrong policy is enforced
perfectly. Nor does it govern what the answerer SAYS once it legitimately holds
a record; that leak class lives past decryption.

WHY IT IS FROZEN. The Developer's tools refuse to write this file, and
tests/test_contract.py checks that every released text was opened through
`open_for` with the requester's identity. Enforcement that can be edited by
the thing it constrains is not enforcement.

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
    PRIMARY KEY (record_id, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_record_keys_principal ON record_keys(principal_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    principal_key(conn, OWNER)




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


def _wrap(conn: sqlite3.Connection, record_id: str, principal_id: str, data_key: bytes) -> None:
    pk = principal_key(conn, principal_id)
    conn.execute(
        "INSERT OR REPLACE INTO record_keys(record_id, principal_id, wrapped, granted_at) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (record_id, principal_id, _keystream(pk, record_id, data_key)),
    )



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


def grant(conn: sqlite3.Connection, record_id: str, principal_id: str) -> bool:
    """Wrap the record's data key for `principal_id`. False if the record is shredded."""
    data_key = _unwrap(conn, record_id, OWNER)
    if data_key is None:
        return False
    _wrap(conn, record_id, principal_id, data_key)
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
