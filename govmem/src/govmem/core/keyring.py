"""Per-principal key wrapping: the only path to plaintext (SPEC §3 rule 3). Frozen.

Each sealed item belongs to a *key id* (a record's full key `r00012`, its redacted key `r00012#r`).
The key id's data key exists only wrapped: once for OWNER (the store's maintenance identity) and once
per principal granted. `open_for` needs a wrap for the asking principal. `shred` destroys every wrap,
OWNER's included, so ciphertext under that key is unrecoverable.

Items under one key use distinct nonces (`body`, `note:3`, `attr:17`) so the keystream is never reused.
The cipher is an HMAC-SHA256 counter keystream (stdlib only). Not authenticated; not for production.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3

OWNER = "__store__"

SCHEMA = """
CREATE TABLE IF NOT EXISTS principal_keys (principal_id TEXT PRIMARY KEY, key_material BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS _keys (
    key_id TEXT NOT NULL, principal_id TEXT NOT NULL, wrapped BLOB NOT NULL,
    PRIMARY KEY (key_id, principal_id));
CREATE INDEX IF NOT EXISTS idx_keys_principal ON _keys(principal_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    principal_key(conn, OWNER)


def _stream(key: bytes, nonce: str, data: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        out += hmac.new(key, f"{nonce}:{counter}".encode(), hashlib.sha256).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[: len(data)]))


def principal_key(conn: sqlite3.Connection, principal_id: str) -> bytes:
    row = conn.execute("SELECT key_material FROM principal_keys WHERE principal_id=?", (principal_id,)).fetchone()
    if row is not None:
        return bytes(row[0])
    key = os.urandom(32)
    conn.execute("INSERT INTO principal_keys(principal_id, key_material) VALUES (?, ?)", (principal_id, key))
    return key


def _wrap(conn: sqlite3.Connection, key_id: str, principal_id: str, data_key: bytes) -> None:
    pk = principal_key(conn, principal_id)
    conn.execute("INSERT OR REPLACE INTO _keys(key_id, principal_id, wrapped) VALUES (?, ?, ?)",
                 (key_id, principal_id, _stream(pk, "wrap:" + key_id, data_key)))


def _unwrap(conn: sqlite3.Connection, key_id: str, principal_id: str) -> bytes | None:
    row = conn.execute("SELECT wrapped FROM _keys WHERE key_id=? AND principal_id=?", (key_id, principal_id)).fetchone()
    if row is None:
        return None
    return _stream(principal_key(conn, principal_id), "wrap:" + key_id, bytes(row[0]))


def new_key(conn: sqlite3.Connection, key_id: str) -> None:
    """Create a data key for `key_id`, wrapped for OWNER only."""
    if _unwrap(conn, key_id, OWNER) is None:
        _wrap(conn, key_id, OWNER, os.urandom(32))


def seal(conn: sqlite3.Connection, key_id: str, nonce: str, plaintext: str) -> bytes | None:
    data_key = _unwrap(conn, key_id, OWNER)
    if data_key is None:
        return None
    return _stream(data_key, f"{key_id}|{nonce}", plaintext.encode("utf-8"))


def open_for(conn: sqlite3.Connection, key_id: str, principal_id: str, nonce: str,
             ciphertext: bytes | None) -> str | None:
    """The plaintext if and only if `principal_id` holds a wrap for `key_id`."""
    if not ciphertext:
        return None
    data_key = _unwrap(conn, key_id, principal_id)
    if data_key is None:
        return None
    return _stream(data_key, f"{key_id}|{nonce}", bytes(ciphertext)).decode("utf-8", "replace")


def grant(conn: sqlite3.Connection, key_id: str, principal_id: str) -> bool:
    data_key = _unwrap(conn, key_id, OWNER)
    if data_key is None:
        return False
    _wrap(conn, key_id, principal_id, data_key)
    return True


def revoke(conn: sqlite3.Connection, key_id: str, principal_id: str) -> None:
    if principal_id == OWNER:
        return
    conn.execute("DELETE FROM _keys WHERE key_id=? AND principal_id=?", (key_id, principal_id))


def holders(conn: sqlite3.Connection, key_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT principal_id FROM _keys WHERE key_id=?", (key_id,))} - {OWNER}


def shred(conn: sqlite3.Connection, key_id: str) -> None:
    conn.execute("DELETE FROM _keys WHERE key_id=?", (key_id,))
