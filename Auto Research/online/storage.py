"""SQLite is authoritative; JSON artifacts are reproducible materialized views."""
import hashlib
import json
import sqlite3
from pathlib import Path


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(encode(value) + '\n')
    tmp.replace(path)


class Journal:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        # Custom checkpointer paths can live outside the private run directory.
        # Set the database mode before WAL creation or sensitive payload writes;
        # SQLite derives new WAL/SHM permissions from the database file.
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, cursor INTEGER NOT NULL, memory BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS events (idx INTEGER PRIMARY KEY, phase TEXT NOT NULL, value TEXT NOT NULL, snapshot BLOB);
        CREATE TABLE IF NOT EXISTS official (id TEXT PRIMARY KEY, boundary INTEGER NOT NULL, version TEXT NOT NULL,
                                            prediction TEXT NOT NULL, score TEXT);
        CREATE TRIGGER IF NOT EXISTS immutable_prediction BEFORE UPDATE OF prediction, version, boundary ON official
            BEGIN SELECT RAISE(ABORT, 'official first attempt is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_score BEFORE UPDATE OF score ON official WHEN OLD.score IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'official score is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_official_delete BEFORE DELETE ON official
            BEGIN SELECT RAISE(ABORT, 'official first attempt is immutable'); END;
        CREATE TABLE IF NOT EXISTS attempts (boundary INTEGER, idx INTEGER, status TEXT, value TEXT,
                                             PRIMARY KEY(boundary, idx));
        CREATE TABLE IF NOT EXISTS repairs (boundary INTEGER, attempt INTEGER, id TEXT, value TEXT,
                                            PRIMARY KEY(boundary, attempt, id));
        CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, retries INTEGER NOT NULL DEFAULT 0,
                                               tokens INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS replay_votes (
            cache_key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS unresolved_data (
            boundary INTEGER PRIMARY KEY, episode_id TEXT NOT NULL, first_position INTEGER NOT NULL,
            last_position INTEGER NOT NULL, payload TEXT NOT NULL, reason TEXT NOT NULL,
            version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unresolved', retries INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS safe_images (
            promotion TEXT NOT NULL, parent TEXT NOT NULL, episode_id TEXT NOT NULL,
            cursor INTEGER NOT NULL, memory BLOB NOT NULL, PRIMARY KEY(promotion,episode_id));
        CREATE TABLE IF NOT EXISTS safe_state (promotion TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS backfill_results (
            boundary INTEGER NOT NULL, checkpoint_id TEXT NOT NULL, version TEXT NOT NULL,
            value TEXT NOT NULL, PRIMARY KEY(boundary,checkpoint_id,version));
        ''')
        if 'invocations' not in {r[1] for r in self.db.execute('PRAGMA table_info(operations)')}:
            self.db.execute('ALTER TABLE operations ADD COLUMN invocations INTEGER NOT NULL DEFAULT 0')
            self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (key, encode(value)))

    def event(self, index):
        row = self.db.execute('SELECT * FROM events WHERE idx=?', (index,)).fetchone()
        return (row['phase'], json.loads(row['value']), row['snapshot']) if row else ('new', {}, None)

    def save_event(self, index, phase, value, snapshot=None):
        self.db.execute('INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?)',
                        (index, phase, encode(value), snapshot))

    def official(self):
        return [{'checkpoint_id': r['id'], 'event_boundary_index': r['boundary'],
                     'frozen_system_version': r['version'], 'attempt_kind': 'official_first_attempt',
                     'prediction': json.loads(r['prediction']), 'score': json.loads(r['score']) if r['score'] else None}
                for r in self.db.execute('SELECT * FROM official ORDER BY boundary, id')]

    def replay_vote(self, cache_key):
        row = self.db.execute(
            'SELECT value FROM replay_votes WHERE cache_key=?', (cache_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_replay_vote(self, cache_key, value, created_at):
        self.db.execute(
            'INSERT OR REPLACE INTO replay_votes VALUES (?,?,?)',
            (cache_key, encode(value), float(created_at)))

    def close(self):
        self.db.close()
