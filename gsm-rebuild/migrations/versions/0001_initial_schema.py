"""Initial Governed Shared Memory schema.

Every tenant-owned table carries tenant_id and references its parents through
composite (tenant_id, ...) foreign keys, so a row can never point at another
tenant's row. Row-level security is enabled and forced as defense in depth;
the application still filters by tenant on every statement.

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

VIEWS = "ARRAY['full','redacted','aggregate','existence_only']::text[]"

TENANT_TABLES = [
    "workspaces",
    "principals",
    "roles",
    "principal_roles",
    "relationships",
    "raw_events",
    "memory_atoms",
    "memory_versions",
    "memory_edges",
    "memory_projections",
    "embeddings",
    "explicit_grants",
    "explicit_denials",
    "deletion_tombstones",
    "deletion_cascades",
    "commit_receipts",
    "policy_decisions",
    "release_audits",
]

APPEND_ONLY_TABLES = ["policy_decisions", "release_audits", "commit_receipts"]

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE tenants (
    tenant_id    text PRIMARY KEY CHECK (tenant_id ~ '^[A-Za-z0-9_.:-]{{1,128}}$'),
    display_name text NOT NULL,
    created_at   timestamptz NOT NULL
);

CREATE TABLE workspaces (
    tenant_id    text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    workspace_id text NOT NULL CHECK (workspace_id ~ '^[A-Za-z0-9_.:-]{{1,128}}$'),
    display_name text NOT NULL,
    created_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id)
);

CREATE TABLE principals (
    tenant_id      text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    principal_id   text NOT NULL CHECK (principal_id ~ '^[A-Za-z0-9_.:@-]{{1,128}}$'),
    display_name   text NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('human', 'agent', 'service')),
    attributes     jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL,
    deactivated_at timestamptz,
    PRIMARY KEY (tenant_id, principal_id)
);

CREATE TABLE roles (
    tenant_id   text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    role_name   text NOT NULL CHECK (role_name ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, role_name)
);

CREATE TABLE principal_roles (
    tenant_id    text NOT NULL,
    principal_id text NOT NULL,
    role_name    text NOT NULL,
    valid_from   timestamptz NOT NULL,
    valid_until  timestamptz,
    revoked_at   timestamptz,
    granted_by   text NOT NULL,
    created_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, principal_id, role_name),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, principal_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, role_name) REFERENCES roles (tenant_id, role_name) ON DELETE CASCADE,
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);
CREATE INDEX principal_roles_active ON principal_roles (tenant_id, principal_id) WHERE revoked_at IS NULL;

CREATE TABLE relationships (
    tenant_id            text NOT NULL,
    relationship_id      text NOT NULL,
    subject_principal_id text NOT NULL,
    relation             text NOT NULL CHECK (relation ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    object_type          text NOT NULL CHECK (object_type ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    object_id            text NOT NULL,
    valid_from           timestamptz NOT NULL,
    valid_until          timestamptz,
    removed_at           timestamptz,
    created_by           text NOT NULL,
    created_at           timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, relationship_id),
    FOREIGN KEY (tenant_id, subject_principal_id) REFERENCES principals (tenant_id, principal_id) ON DELETE CASCADE,
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);
CREATE UNIQUE INDEX relationships_active_uq
    ON relationships (tenant_id, subject_principal_id, relation, object_type, object_id)
    WHERE removed_at IS NULL;
CREATE INDEX relationships_active_by_object
    ON relationships (tenant_id, object_type, object_id, relation)
    WHERE removed_at IS NULL;

CREATE TABLE raw_events (
    tenant_id           text NOT NULL,
    workspace_id        text NOT NULL,
    event_id            text NOT NULL,
    source_principal_id text NOT NULL,
    event_type          text NOT NULL,
    observed_at         timestamptz NOT NULL,
    received_at         timestamptz NOT NULL,
    payload             jsonb,
    payload_state       text NOT NULL CHECK (payload_state IN ('present', 'none', 'scrubbed')),
    payload_scrubbed_at timestamptz,
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, source_principal_id) REFERENCES principals (tenant_id, principal_id),
    CHECK (payload_state <> 'scrubbed' OR payload IS NULL)
);

CREATE TABLE memory_atoms (
    tenant_id               text NOT NULL,
    workspace_id            text NOT NULL,
    memory_id               text NOT NULL,
    semantic_type           text NOT NULL CHECK (semantic_type IN
                              ('fact', 'preference', 'decision', 'task', 'permission', 'summary', 'report',
                               'availability', 'note', 'credential')),
    content_schema          text NOT NULL,
    attribute               text,
    semantic_key            text,
    topic                   text NOT NULL,
    subject_ids             text[] NOT NULL DEFAULT '{{}}',
    owner_ids               text[] NOT NULL DEFAULT '{{}}',
    source_event_id         text,
    source_principal_id     text NOT NULL,
    sensitivity             text NOT NULL CHECK (sensitivity IN ('public', 'internal', 'confidential', 'restricted')),
    compartments            text[] NOT NULL DEFAULT '{{}}',
    policy                  jsonb NOT NULL,
    policy_revision         integer NOT NULL DEFAULT 1 CHECK (policy_revision >= 1),
    allowed_views           text[] NOT NULL CHECK (cardinality(allowed_views) >= 1 AND allowed_views <@ {VIEWS}),
    access_tags             text[] NOT NULL DEFAULT '{{}}',
    existence_tags          text[] NOT NULL DEFAULT '{{}}',
    lifecycle_state         text NOT NULL DEFAULT 'active'
                              CHECK (lifecycle_state IN ('active', 'expired', 'pending_invalidation', 'tombstoned')),
    retain_until            timestamptz,
    is_derived              boolean NOT NULL DEFAULT false,
    inherits_parent_policy  boolean NOT NULL DEFAULT false,
    derivation_rule         text,
    superseded_by_memory_id text,
    observed_at             timestamptz NOT NULL,
    created_at              timestamptz NOT NULL,
    tombstoned_at           timestamptz,
    PRIMARY KEY (tenant_id, memory_id),
    UNIQUE (tenant_id, workspace_id, memory_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, source_principal_id) REFERENCES principals (tenant_id, principal_id),
    FOREIGN KEY (tenant_id, source_event_id) REFERENCES raw_events (tenant_id, event_id),
    CHECK (NOT inherits_parent_policy OR is_derived),
    CHECK ((lifecycle_state = 'tombstoned') = (tombstoned_at IS NOT NULL))
);
CREATE INDEX memory_atoms_active ON memory_atoms (tenant_id, workspace_id) WHERE lifecycle_state = 'active';
CREATE INDEX memory_atoms_lifecycle ON memory_atoms (tenant_id, lifecycle_state);
CREATE INDEX memory_atoms_subjects ON memory_atoms USING GIN (subject_ids);
CREATE INDEX memory_atoms_owners ON memory_atoms USING GIN (owner_ids);
CREATE INDEX memory_atoms_compartments ON memory_atoms USING GIN (compartments);
CREATE INDEX memory_atoms_access_tags ON memory_atoms USING GIN (access_tags);
CREATE INDEX memory_atoms_existence_tags ON memory_atoms USING GIN (existence_tags);

CREATE TABLE memory_versions (
    tenant_id             text NOT NULL,
    workspace_id          text NOT NULL,
    version_id            text NOT NULL,
    memory_id             text NOT NULL,
    semantic_key          text,
    content_text          text,
    content_fields        jsonb,
    content_scrubbed_at   timestamptz,
    valid_from            timestamptz NOT NULL,
    valid_until           timestamptz,
    state                 text NOT NULL CHECK (state IN ('active', 'superseded', 'expired', 'tombstoned')),
    truth_state           text NOT NULL CHECK (truth_state IN ('current', 'contradicted', 'stale', 'superseded')),
    supersedes_version_id text,
    extraction_confidence real NOT NULL CHECK (extraction_confidence >= 0 AND extraction_confidence <= 1),
    origin                text NOT NULL CHECK (origin IN ('oracle', 'model', 'derived')),
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL,
    tombstoned_at         timestamptz,
    PRIMARY KEY (tenant_id, version_id),
    FOREIGN KEY (tenant_id, workspace_id, memory_id)
        REFERENCES memory_atoms (tenant_id, workspace_id, memory_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, supersedes_version_id) REFERENCES memory_versions (tenant_id, version_id),
    CHECK (valid_until IS NULL OR valid_until >= valid_from),
    CHECK ((state = 'tombstoned') = (tombstoned_at IS NOT NULL)),
    CHECK (content_scrubbed_at IS NULL OR (content_text IS NULL AND content_fields IS NULL)),
    CHECK (content_scrubbed_at IS NULL OR state = 'tombstoned')
);
-- At most one active version per memory, and per single-valued semantic identity.
CREATE UNIQUE INDEX memory_versions_one_active ON memory_versions (tenant_id, memory_id) WHERE state = 'active';
CREATE UNIQUE INDEX memory_versions_semantic_key_active
    ON memory_versions (tenant_id, workspace_id, semantic_key)
    WHERE state = 'active' AND semantic_key IS NOT NULL;
CREATE INDEX memory_versions_by_memory ON memory_versions (tenant_id, memory_id, valid_from DESC);
CREATE INDEX memory_versions_supersedes ON memory_versions (tenant_id, supersedes_version_id);

CREATE TABLE memory_edges (
    tenant_id     text NOT NULL,
    edge_id       text NOT NULL,
    edge_type     text NOT NULL CHECK (edge_type IN
                    ('source_event', 'derived_from', 'summary_of', 'report_of', 'supersedes', 'contradicts')),
    src_memory_id text,
    src_event_id  text,
    dst_memory_id text NOT NULL,
    created_at    timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, edge_id),
    FOREIGN KEY (tenant_id, src_memory_id) REFERENCES memory_atoms (tenant_id, memory_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, src_event_id) REFERENCES raw_events (tenant_id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, dst_memory_id) REFERENCES memory_atoms (tenant_id, memory_id) ON DELETE CASCADE,
    CHECK ((src_memory_id IS NULL) <> (src_event_id IS NULL)),
    CHECK ((edge_type = 'source_event') = (src_event_id IS NOT NULL)),
    CHECK (src_memory_id IS DISTINCT FROM dst_memory_id)
);
CREATE UNIQUE INDEX memory_edges_memory_uq ON memory_edges (tenant_id, edge_type, src_memory_id, dst_memory_id)
    WHERE src_memory_id IS NOT NULL;
CREATE UNIQUE INDEX memory_edges_event_uq ON memory_edges (tenant_id, src_event_id, dst_memory_id)
    WHERE src_event_id IS NOT NULL;
CREATE INDEX memory_edges_children ON memory_edges (tenant_id, src_memory_id, edge_type);
CREATE INDEX memory_edges_parents ON memory_edges (tenant_id, dst_memory_id, edge_type);

-- Lexical index material: one row per (version, disclosure view). Ranking only
-- ever reads the projection for the view the requester is permitted, so hidden
-- fields never influence scores.
CREATE TABLE memory_projections (
    tenant_id  text NOT NULL,
    memory_id  text NOT NULL,
    version_id text NOT NULL,
    view       text NOT NULL CHECK (view = ANY ({VIEWS})),
    fields     jsonb NOT NULL,
    text       text NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, version_id, view),
    FOREIGN KEY (tenant_id, version_id) REFERENCES memory_versions (tenant_id, version_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, memory_id) REFERENCES memory_atoms (tenant_id, memory_id) ON DELETE CASCADE
);
CREATE INDEX memory_projections_tsv ON memory_projections USING GIN (tsv);
CREATE INDEX memory_projections_memory ON memory_projections (tenant_id, memory_id);

CREATE TABLE embeddings (
    tenant_id    text NOT NULL,
    embedding_id text NOT NULL,
    memory_id    text NOT NULL,
    version_id   text NOT NULL,
    view         text NOT NULL,
    model        text NOT NULL,
    dim          integer NOT NULL CHECK (dim > 0),
    embedding    vector NOT NULL,
    created_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, embedding_id),
    UNIQUE (tenant_id, version_id, view, model),
    FOREIGN KEY (tenant_id, version_id, view)
        REFERENCES memory_projections (tenant_id, version_id, view) ON DELETE CASCADE,
    CHECK (vector_dims(embedding) = dim)
);
CREATE INDEX embeddings_by_memory ON embeddings (tenant_id, memory_id);
-- ANN index for the default fake/deterministic dimension. Retrieval ranks
-- exactly within the authorized subset; see docs/RETRIEVAL.md.
CREATE INDEX embeddings_hnsw_256 ON embeddings
    USING hnsw ((embedding::vector(256)) vector_cosine_ops) WHERE dim = 256;

CREATE TABLE explicit_grants (
    tenant_id            text NOT NULL,
    grant_id             text NOT NULL,
    resource_type        text NOT NULL CHECK (resource_type IN ('memory', 'compartment')),
    memory_id            text,
    compartment          text,
    grantee_principal_id text NOT NULL,
    view                 text CHECK (view IS NULL OR view = ANY ({VIEWS})),
    purposes             text[] NOT NULL DEFAULT '{{}}',
    valid_from           timestamptz NOT NULL,
    expires_at           timestamptz,
    revoked_at           timestamptz,
    revoked_by           text,
    granted_by           text NOT NULL,
    created_at           timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, grant_id),
    FOREIGN KEY (tenant_id, memory_id) REFERENCES memory_atoms (tenant_id, memory_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, grantee_principal_id) REFERENCES principals (tenant_id, principal_id) ON DELETE CASCADE,
    CHECK ((resource_type = 'memory') = (memory_id IS NOT NULL)),
    CHECK ((resource_type = 'compartment') = (compartment IS NOT NULL)),
    CHECK (resource_type <> 'memory' OR view IS NOT NULL),
    CHECK (expires_at IS NULL OR expires_at > valid_from)
);
CREATE INDEX explicit_grants_active_grantee ON explicit_grants (tenant_id, grantee_principal_id)
    WHERE revoked_at IS NULL;
CREATE INDEX explicit_grants_active_memory ON explicit_grants (tenant_id, memory_id)
    WHERE revoked_at IS NULL;

CREATE TABLE explicit_denials (
    tenant_id     text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    denial_id     text NOT NULL,
    resource_type text NOT NULL CHECK (resource_type IN ('memory', 'compartment', 'subject')),
    resource_id   text NOT NULL,
    principal_id  text NOT NULL,
    reason_code   text NOT NULL,
    valid_from    timestamptz NOT NULL,
    valid_until   timestamptz,
    lifted_at     timestamptz,
    created_by    text NOT NULL,
    created_at    timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, denial_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, principal_id) ON DELETE CASCADE
);
CREATE INDEX explicit_denials_active ON explicit_denials (tenant_id, principal_id, resource_type, resource_id)
    WHERE lifted_at IS NULL;

-- Content-free deletion receipts. No content, no content hashes.
CREATE TABLE deletion_tombstones (
    tenant_id           text NOT NULL,
    tombstone_id        text NOT NULL,
    memory_id           text NOT NULL,
    requested_by        text NOT NULL,
    reason_code         text NOT NULL,
    cause               text NOT NULL CHECK (cause IN ('direct', 'cascade')),
    parent_tombstone_id text,
    correlation_id      text NOT NULL,
    versions_tombstoned integer NOT NULL CHECK (versions_tombstoned >= 0),
    policy_version      text NOT NULL,
    created_at          timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, tombstone_id),
    UNIQUE (tenant_id, memory_id),
    FOREIGN KEY (tenant_id, memory_id) REFERENCES memory_atoms (tenant_id, memory_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, parent_tombstone_id) REFERENCES deletion_tombstones (tenant_id, tombstone_id)
);

CREATE TABLE deletion_cascades (
    tenant_id              text NOT NULL,
    cascade_id             text NOT NULL,
    tombstone_id           text NOT NULL,
    status                 text NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    attempts               integer NOT NULL DEFAULT 0,
    descendants_found      integer NOT NULL DEFAULT 0,
    descendants_tombstoned integer NOT NULL DEFAULT 0,
    versions_scrubbed      integer NOT NULL DEFAULT 0,
    projections_deleted    integer NOT NULL DEFAULT 0,
    embeddings_deleted     integer NOT NULL DEFAULT 0,
    raw_events_scrubbed    integer NOT NULL DEFAULT 0,
    cache_evicted          boolean NOT NULL DEFAULT false,
    last_error_code        text,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    completed_at           timestamptz,
    PRIMARY KEY (tenant_id, cascade_id),
    UNIQUE (tenant_id, tombstone_id),
    FOREIGN KEY (tenant_id, tombstone_id) REFERENCES deletion_tombstones (tenant_id, tombstone_id) ON DELETE CASCADE
);
CREATE INDEX deletion_cascades_open ON deletion_cascades (tenant_id, status) WHERE status <> 'completed';

CREATE TABLE commit_receipts (
    tenant_id         text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    receipt_id        text NOT NULL,
    correlation_id    text NOT NULL,
    principal_id      text NOT NULL,
    status            text NOT NULL CHECK (status IN ('committed', 'rejected', 'rolled_back', 'partial_quarantine', 'noop')),
    operation_count   integer NOT NULL,
    committed_count   integer NOT NULL,
    rejected_count    integer NOT NULL,
    quarantined_count integer NOT NULL,
    memory_ids        text[] NOT NULL DEFAULT '{{}}',
    reason_codes      text[] NOT NULL DEFAULT '{{}}',
    policy_version    text NOT NULL,
    created_at        timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, receipt_id)
);

CREATE TABLE policy_decisions (
    tenant_id      text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    decision_id    text NOT NULL,
    correlation_id text NOT NULL,
    principal_id   text NOT NULL,
    operation      text NOT NULL,
    memory_id      text,
    decision       text NOT NULL CHECK (decision IN ('ALLOW', 'ALLOW_REDACTED', 'DENY', 'UNAVAILABLE', 'ESCALATE')),
    view           text,
    reason_codes   text[] NOT NULL,
    policy_version text NOT NULL,
    decided_at     timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, decision_id)
);
CREATE INDEX policy_decisions_correlation ON policy_decisions (tenant_id, correlation_id);
CREATE INDEX policy_decisions_memory ON policy_decisions (tenant_id, memory_id);

CREATE TABLE release_audits (
    tenant_id           text NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    release_id          text NOT NULL,
    correlation_id      text NOT NULL,
    principal_id        text NOT NULL,
    operation           text NOT NULL,
    action              text NOT NULL CHECK (action IN ('ANSWER', 'ANSWER_REDACTED', 'REFUSE', 'NO_MEMORY', 'ESCALATE')),
    approved_memory_ids text[] NOT NULL DEFAULT '{{}}',
    blocked_memory_ids  text[] NOT NULL DEFAULT '{{}}',
    withheld_count      integer NOT NULL DEFAULT 0,
    reason_codes        text[] NOT NULL,
    policy_version      text NOT NULL,
    query_fingerprint   text NOT NULL,
    generator           text NOT NULL,
    verifier_confidence real,
    latency_ms          integer NOT NULL,
    prompt_tokens       integer NOT NULL DEFAULT 0,
    completion_tokens   integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, release_id)
);
CREATE INDEX release_audits_correlation ON release_audits (tenant_id, correlation_id);
"""

TRIGGERS_SQL = """
-- Version content is immutable: it may only ever be scrubbed to NULL by the
-- deletion cascade, never rewritten. Tombstoning is terminal.
CREATE FUNCTION gsm_guard_memory_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.memory_id IS DISTINCT FROM OLD.memory_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.semantic_key IS DISTINCT FROM OLD.semantic_key
       OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
       OR NEW.supersedes_version_id IS DISTINCT FROM OLD.supersedes_version_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'GSM_IMMUTABLE_VERSION' USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.content_text IS DISTINCT FROM OLD.content_text AND NEW.content_text IS NOT NULL)
       OR (NEW.content_fields IS DISTINCT FROM OLD.content_fields AND NEW.content_fields IS NOT NULL) THEN
        RAISE EXCEPTION 'GSM_IMMUTABLE_VERSION' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.state = 'tombstoned' AND NEW.state <> 'tombstoned' THEN
        RAISE EXCEPTION 'GSM_TOMBSTONE_TERMINAL' USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.content_text IS NULL AND OLD.content_text IS NOT NULL) AND NEW.state <> 'tombstoned' THEN
        RAISE EXCEPTION 'GSM_SCRUB_REQUIRES_TOMBSTONE' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER memory_versions_guard BEFORE UPDATE ON memory_versions
    FOR EACH ROW EXECUTE FUNCTION gsm_guard_memory_version();

CREATE FUNCTION gsm_guard_memory_atom() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.memory_id IS DISTINCT FROM OLD.memory_id THEN
        RAISE EXCEPTION 'GSM_IMMUTABLE_ATOM_IDENTITY' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.lifecycle_state = 'tombstoned' AND NEW.lifecycle_state <> 'tombstoned' THEN
        RAISE EXCEPTION 'GSM_TOMBSTONE_TERMINAL' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER memory_atoms_guard BEFORE UPDATE ON memory_atoms
    FOR EACH ROW EXECUTE FUNCTION gsm_guard_memory_atom();

-- No new version (or re-activation) can be attached to a tombstoned memory.
CREATE FUNCTION gsm_guard_version_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM memory_atoms a
        WHERE a.tenant_id = NEW.tenant_id AND a.memory_id = NEW.memory_id AND a.lifecycle_state = 'tombstoned'
    ) THEN
        RAISE EXCEPTION 'GSM_TOMBSTONE_TERMINAL' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER memory_versions_insert_guard BEFORE INSERT ON memory_versions
    FOR EACH ROW EXECUTE FUNCTION gsm_guard_version_insert();

-- Reindexing can never resurrect deleted content: index material may only be
-- written for versions and atoms that are not tombstoned.
CREATE FUNCTION gsm_guard_index_material() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM memory_versions v
        JOIN memory_atoms a ON a.tenant_id = v.tenant_id AND a.memory_id = v.memory_id
        WHERE v.tenant_id = NEW.tenant_id AND v.version_id = NEW.version_id
          AND (v.state = 'tombstoned' OR a.lifecycle_state = 'tombstoned')
    ) THEN
        RAISE EXCEPTION 'GSM_INDEX_TOMBSTONED' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER memory_projections_guard BEFORE INSERT OR UPDATE ON memory_projections
    FOR EACH ROW EXECUTE FUNCTION gsm_guard_index_material();
CREATE TRIGGER embeddings_guard BEFORE INSERT OR UPDATE ON embeddings
    FOR EACH ROW EXECUTE FUNCTION gsm_guard_index_material();
"""


def _rls_sql() -> str:
    parts = [
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gsm_app') THEN
        CREATE ROLE gsm_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    EXECUTE format('GRANT gsm_app TO %I', current_user);
END $$;
GRANT USAGE ON SCHEMA public TO gsm_app;
-- Tenant catalog: no RLS (it holds no tenant data); the admin API creates and resets tenants.
GRANT SELECT, INSERT, DELETE ON tenants TO gsm_app;
"""
    ]
    for table in TENANT_TABLES:
        if table in APPEND_ONLY_TABLES:
            parts.append(f"GRANT SELECT, INSERT ON {table} TO gsm_app;")
        else:
            parts.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO gsm_app;")
        parts.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        parts.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        parts.append(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"USING (tenant_id = current_setting('gsm.tenant_id', true)) "
            f"WITH CHECK (tenant_id = current_setting('gsm.tenant_id', true));"
        )
    return "\n".join(parts)


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    op.execute(TRIGGERS_SQL)
    op.execute(_rls_sql())


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS gsm_guard_memory_version() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS gsm_guard_memory_atom() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS gsm_guard_index_material() CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS gsm_guard_version_insert() CASCADE;")
