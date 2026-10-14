-- Generic, evidence-governed memory. The schema contains no domain roles,
-- relationship labels, fact types, or grants. Those are learned values.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS principals (
    principal_id TEXT PRIMARY KEY,
    observed_role TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    source_observation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    observer_id TEXT NOT NULL,
    observed_role TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    seq INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE (episode_id, turn_id)
);

-- Relationship predicates are data, and never imply access.
CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    valid_from TEXT,
    valid_until TEXT,
    source_observation_id TEXT,
    derived_from_id TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    supersedes_id TEXT
);

-- Facts, events, restrictions, interpretations, and transitions share one
-- extensible shape. Unseen concepts require no schema migration.
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    predicate TEXT NOT NULL,
    value_json TEXT NOT NULL,
    text_value TEXT NOT NULL DEFAULT '',
    concepts_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    valid_from TEXT,
    valid_until TEXT,
    source_observation_id TEXT NOT NULL,
    derived_from_id TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    seq INTEGER NOT NULL,
    supersedes_id TEXT,
    FOREIGN KEY (source_observation_id) REFERENCES observations(observation_id)
);

CREATE TABLE IF NOT EXISTS permissions (
    permission_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    grantee_id TEXT NOT NULL,
    resource_owner_id TEXT NOT NULL,
    effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    scope_json TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    exclusions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    valid_from TEXT,
    valid_until TEXT,
    source_observation_id TEXT NOT NULL,
    derived_from_id TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    supersedes_id TEXT,
    FOREIGN KEY (source_observation_id) REFERENCES observations(observation_id)
);

-- Semantic links influence retrieval only, never authorization.
CREATE TABLE IF NOT EXISTS semantic_links (
    link_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    original_value TEXT NOT NULL,
    related_value TEXT NOT NULL,
    relation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_observation_id TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_observation_id) REFERENCES observations(observation_id)
);

CREATE TABLE IF NOT EXISTS memory_terms (
    memory_id TEXT NOT NULL,
    term_hash TEXT NOT NULL,
    PRIMARY KEY (memory_id, term_hash),
    FOREIGN KEY (memory_id) REFERENCES memories(memory_id)
);

CREATE TABLE IF NOT EXISTS tombstones (
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    shredded INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (target_id, target_type)
);

CREATE TABLE IF NOT EXISTS access_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id TEXT,
    requester_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value BLOB);

CREATE INDEX IF NOT EXISTS idx_observations_episode_seq ON observations(episode_id, seq);
CREATE INDEX IF NOT EXISTS idx_memories_episode_seq ON memories(episode_id, seq);
CREATE INDEX IF NOT EXISTS idx_memories_state ON memories(episode_id, status, valid_until);
CREATE INDEX IF NOT EXISTS idx_permissions_lookup ON permissions(episode_id, grantee_id, resource_owner_id, status);
CREATE INDEX IF NOT EXISTS idx_relationships_subject ON relationships(episode_id, subject_id, object_id);
CREATE INDEX IF NOT EXISTS idx_memory_terms_hash ON memory_terms(term_hash);
