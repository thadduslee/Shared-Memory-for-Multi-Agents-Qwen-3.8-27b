"""The fixed tables (SPEC §6.1). No table or column is created during a conversation."""

DDL = """
PRAGMA secure_delete = ON;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS principals (id TEXT PRIMARY KEY, role TEXT, display_name TEXT, kind TEXT, acts_for TEXT);
CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, kind_label TEXT);
CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY, type TEXT, endpoints TEXT, principals TEXT, entities TEXT, attributes TEXT,
    qualified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS turns (seq INTEGER PRIMARY KEY, turn_id TEXT UNIQUE, ts TEXT, speaker_id TEXT);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY, body BLOB, speaker_id TEXT NOT NULL, source_turn TEXT NOT NULL,
    created_seq INTEGER NOT NULL, created_at TEXT, valid_from TEXT, valid_to TEXT,
    superseded_by TEXT, superseded_seq INTEGER, slot TEXT, derived_from TEXT,
    tombstoned_at TEXT, kind TEXT, has_redacted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_records_slot ON records(slot);
CREATE TABLE IF NOT EXISTS record_subjects (
    record_id TEXT NOT NULL, subject_id TEXT NOT NULL, is_owner INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (record_id, subject_id));
CREATE INDEX IF NOT EXISTS idx_subjects_subject ON record_subjects(subject_id);
CREATE TABLE IF NOT EXISTS record_views (
    record_id TEXT NOT NULL, principal_id TEXT NOT NULL, view TEXT NOT NULL, PRIMARY KEY (record_id, principal_id));
CREATE INDEX IF NOT EXISTS idx_views_principal ON record_views(principal_id);
CREATE TABLE IF NOT EXISTS record_redacted (record_id TEXT PRIMARY KEY, body BLOB);
CREATE TABLE IF NOT EXISTS protected_values (record_id TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_protected_record ON protected_values(record_id);
CREATE TABLE IF NOT EXISTS notes (
    record_id TEXT NOT NULL, version INTEGER NOT NULL, keywords BLOB, tags BLOB, context BLOB,
    status TEXT NOT NULL, reorg_id TEXT, PRIMARY KEY (record_id, version));
CREATE TABLE IF NOT EXISTS attributes (
    id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL, value BLOB,
    status TEXT NOT NULL, reorg_id TEXT);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL, kind TEXT NOT NULL, strength REAL,
    status TEXT NOT NULL, reorg_id TEXT);
CREATE INDEX IF NOT EXISTS idx_links_a ON links(a);
CREATE INDEX IF NOT EXISTS idx_links_b ON links(b);
CREATE TABLE IF NOT EXISTS clusters (id TEXT PRIMARY KEY, label BLOB, status TEXT NOT NULL, reorg_id TEXT);
CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id TEXT NOT NULL, record_id TEXT NOT NULL, status TEXT NOT NULL, reorg_id TEXT,
    PRIMARY KEY (cluster_id, record_id));
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT NOT NULL, target_id TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, reorg_id TEXT,
    PRIMARY KEY (alias, target_id));
CREATE TABLE IF NOT EXISTS embeddings (record_id TEXT PRIMARY KEY, note_version INTEGER, vector BLOB);
CREATE TABLE IF NOT EXISTS terms (record_id TEXT NOT NULL, term_hash TEXT NOT NULL, PRIMARY KEY (record_id, term_hash));
CREATE INDEX IF NOT EXISTS idx_terms_hash ON terms(term_hash);
CREATE TABLE IF NOT EXISTS scope_facts (
    id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, principal_id TEXT NOT NULL, view TEXT NOT NULL,
    source_turn TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS _reorgs (
    id TEXT PRIMARY KEY, op TEXT NOT NULL, args TEXT, evidence TEXT, confidence REAL, status TEXT NOT NULL,
    created_turn TEXT, decided_turn TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS _denials (
    principal_id TEXT NOT NULL, target TEXT NOT NULL, created_seq INTEGER NOT NULL, source_turn TEXT);
CREATE INDEX IF NOT EXISTS idx_denials ON _denials(principal_id, target);
CREATE TABLE IF NOT EXISTS _op_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, turn TEXT, op TEXT, args TEXT, authority TEXT, origin TEXT,
    outcome TEXT, reason_code TEXT);
CREATE TABLE IF NOT EXISTS _signals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, turn TEXT, kind TEXT, refs TEXT, resolved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS _health (counter TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS _forgotten (fingerprint TEXT PRIMARY KEY, created_seq INTEGER);
"""

ACCESS_TABLES = ("records", "record_views", "record_subjects", "_keys", "_denials", "scope_facts", "record_redacted",
                 "notes", "attributes", "links", "clusters", "cluster_members", "aliases", "_reorgs")
