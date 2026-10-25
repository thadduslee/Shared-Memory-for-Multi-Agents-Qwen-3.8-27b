"""Two local SQLite vector stores, coordinated by SQLite's super-journal.

Both files must live on one local filesystem. DELETE journals (not WAL) permit
atomic transactions across ATTACHed databases. No vector service is required.
"""
from pathlib import Path
import os
import sqlite3
import uuid

MEMORY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS resources(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, compartment TEXT NOT NULL,
 policy_id TEXT NOT NULL, payload TEXT NOT NULL, attributes TEXT NOT NULL,
 dependencies TEXT NOT NULL, policy_ceiling TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS representations(
 id TEXT PRIMARY KEY REFERENCES resources(id), space TEXT NOT NULL, vector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provenance(
 id TEXT PRIMARY KEY, inputs TEXT NOT NULL, outputs TEXT NOT NULL,
 processor TEXT NOT NULL, manifest_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY, issuer TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_views(view_id TEXT PRIMARY KEY REFERENCES resources(id),source_id TEXT NOT NULL REFERENCES resources(id));
CREATE TABLE IF NOT EXISTS revisions(old_id TEXT PRIMARY KEY REFERENCES resources(id),new_id TEXT NOT NULL REFERENCES resources(id));
CREATE TABLE IF NOT EXISTS evolution_jobs(
 id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, inputs TEXT NOT NULL,
 processor TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, error_code TEXT);
CREATE TABLE IF NOT EXISTS evolution_attempts(
 job_id TEXT NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
 error_code TEXT, diagnostic_id TEXT, prompt_hash TEXT NOT NULL,
 proposal_hash TEXT, elapsed_s REAL NOT NULL, PRIMARY KEY(job_id,attempt));
CREATE INDEX IF NOT EXISTS resources_route ON resources(compartment,kind,state);
'''
PRIVACY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS privacy.policies(id TEXT PRIMARY KEY, revision INTEGER, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS privacy.memberships(group_id TEXT, principal_id TEXT, PRIMARY KEY(group_id,principal_id));
CREATE TABLE IF NOT EXISTS privacy.authority_owners(kind TEXT,id TEXT,issuer TEXT NOT NULL,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS privacy.assignments(
 id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES policies(id), issuer TEXT NOT NULL,
 body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS privacy.access_edges(
 policy_id TEXT NOT NULL REFERENCES policies(id), memory_id TEXT NOT NULL,
 assignment_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(policy_id,memory_id,assignment_id));
CREATE INDEX IF NOT EXISTS privacy.access_target ON access_edges(memory_id);
CREATE TABLE IF NOT EXISTS privacy.privacy_vectors(
 id TEXT PRIMARY KEY REFERENCES policies(id), space TEXT NOT NULL, vector TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS privacy.privacy_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS privacy.assignment_diagnostics(
 id TEXT PRIMARY KEY, issuer TEXT NOT NULL, body TEXT NOT NULL);
'''


def open_pair(path, privacy_path=None):
    path = Path(path).resolve()
    privacy_path = Path(privacy_path or path.with_suffix('.privacy.sqlite')).resolve()
    if path == privacy_path:
        raise ValueError('Privacy and memory require different database files')
    for p in (path, privacy_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    if os.stat(path.parent).st_dev != os.stat(privacy_path.parent).st_dev:
        raise ValueError('Atomic two-store publication requires one local filesystem')
    db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'policies' in tables:
            raise ValueError('Legacy single-store database: migrate a copy with privacy.migrate first')
        paired = db.execute("SELECT value FROM metadata WHERE key='store_pair'").fetchone() if 'metadata' in tables else None
        if paired and not privacy_path.exists():
            raise ValueError('Missing privacy store; restore the complete pair or replay trusted sources')
        db.execute('ATTACH DATABASE ? AS privacy', (str(privacy_path),))
        for schema in ('main', 'privacy'):
            db.execute(f'PRAGMA {schema}.journal_mode=DELETE')
            db.execute(f'PRAGMA {schema}.synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript(MEMORY_SCHEMA + PRIVACY_SCHEMA)
        other = db.execute("SELECT value FROM privacy_metadata WHERE key='store_pair'").fetchone()
        if bool(paired) != bool(other) or (paired and paired[0] != other[0]):
            raise ValueError('Mismatched database pair; restore both files together')
        pair = paired[0] if paired else uuid.uuid4().hex
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('store_pair',?)", (pair,))
        db.execute("INSERT OR IGNORE INTO privacy_metadata VALUES ('store_pair',?)", (pair,))
        db.execute('COMMIT')
        for p in (path, privacy_path):
            p.chmod(0o600)
        return db, path, privacy_path
    except BaseException:
        db.close()
        raise
