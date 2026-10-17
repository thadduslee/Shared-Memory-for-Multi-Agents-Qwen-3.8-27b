# Governed Shared Memory (GSM)

A research MVP of a **temporal, provenance-aware, policy-enforced shared memory** for multiple AI agents.

> Models may interpret information, but all authorization, lifecycle, disclosure and deletion enforcement
> happens in mandatory programmatic layers that agents cannot bypass.

GSM stores small, independently governable **memory atoms** with immutable versions, decides every read and
write with a deterministic in-process policy engine (RBAC + ReBAC + ABAC + grants/denials + compartments),
retrieves only inside a pre-authorized candidate set, discloses schema-defined projections (full / redacted /
aggregate / existence-only), verifies every release deterministically, and forgets through tombstones plus an
idempotent provenance cascade, while keeping audit receipts content-free.

```mermaid
flowchart LR
  A[Agent / app] -->|request + credentials| AUTH[Authenticator]
  AUTH -->|PrincipalContext<br/>tenant, principal, roles,<br/>auth strength, purpose| SVC[GovernedMemoryService]
  subgraph Trusted deterministic core
    SVC --> PE[Policy engine]
    SVC --> RET[Candidate pre-filter<br/>+ ranking in authorized set]
    SVC --> DIS[Disclosure planner<br/>+ projections]
    SVC --> REL[Release verifier]
    SVC --> LC[Versions / provenance /<br/>tombstones / cascade]
    SVC --> AUD[(Content-free audit)]
  end
  RET --> PG[(PostgreSQL 16 + pgvector<br/>forced RLS)]
  LC --> PG
  subgraph Untrusted, optional
    INT[Interpreter model]
    GEN[Generator model]
    SEM[Semantic verifier]
  end
  SVC -. text of one event .-> INT -. proposals only .-> SVC
  DIS -. permitted projections only .-> GEN -. draft .-> REL
  REL -. draft + plan metadata .-> SEM -. stricter-only verdict .-> REL
```

## What is implemented

| Area | Status |
|---|---|
| Domain model: atoms, immutable versions, typed provenance edges, tombstones, cascades, receipts | done |
| PostgreSQL schema + Alembic migration, composite tenant FKs, partial unique indexes, GIN/HNSW/tsvector indexes | done |
| Forced row-level security under a non-superuser `gsm_app` role; DB triggers make tombstones terminal and version content immutable | done |
| Trusted `PrincipalContext`; HMAC dev authenticator (refused in production); trusted in-process authenticator | done |
| Deterministic policy engine with explicit precedence, stable reason codes, policy versions | done |
| Oracle (structured) ingestion; optional model interpretation with quarantine of high-impact proposals | done |
| Temporal model: supersession, contradiction, validity intervals, retention expiry, `as_of`, separately authorized history | done |
| Retrieval: SQL superset pre-filter → exact engine decision → lexical + pgvector ranking inside the authorized set | done |
| Disclosure: schema projections, disclosure plans, REFUSE vs NO_MEMORY, ESCALATE | done |
| Derived memories with live, conservative policy inheritance; authorized declassification | done |
| Forgetting: synchronous tombstones + idempotent cascade (descendants, content, index material, embeddings, raw events, cache) | done |
| Deterministic generator + release verifier; optional OpenAI-compatible generator/interpreter/embedder/semantic verifier | done |
| FastAPI agent API (`/v1`) and separate admin API (`/admin`), OpenAPI at `/docs` | done |
| GateMem adapter (hidden-field stripping, official scorer hand-off, GSM metrics) | done |
| CIMemories adapter: label regeneration (labels unreleased), full-context baseline, upstream `metrics.py` hand-off, GSM retrieval and purpose-gate methods ([docs and results](docs/CIMEMORIES.md)) | done |
| MUMBench adapter: loader + re-implemented scorer from the paper's schema ([docs](docs/MUMBENCH.md)) | done; data unreleased |
| Cryptographic deletion / certified physical erasure | **not implemented** (see [SECURITY](docs/SECURITY.md)) |

## Quickstart

Requirements: Python 3.12, PostgreSQL 16 with pgvector.

```bash
# 1a. Database with Docker
docker compose up -d db
export GSM_DATABASE_URL=postgresql+psycopg://gsm_owner:gsm_dev_only_password@localhost:55432/gsm

# 1b. ...or without Docker/root (conda-forge ships postgresql + pgvector)
conda create -n gsm -c conda-forge python=3.12 postgresql=16 pgvector && conda activate gsm
scripts/local_pg.sh start                 # prints GSM_DATABASE_URL
export GSM_DATABASE_URL=$(scripts/local_pg.sh url)

# 2. Install and migrate
pip install -e ".[dev]"
gsm migrate                               # == alembic upgrade head

# 3. Run the acceptance scenarios against the database
gsm demo

# 4. Run the API (development authenticator, never in production)
export GSM_DEV_AUTH_ENABLED=true GSM_DEV_AUTH_SECRET=$(openssl rand -hex 16) GSM_ADMIN_API_KEY=$(openssl rand -hex 16)
gsm seed --tenant acme
gsm serve --port 8080                     # OpenAPI UI at http://localhost:8080/docs
```

`make help` lists the same commands (`make db`, `make migrate`, `make test`, `make lint`, `make typecheck`,
`make demo`, `make serve`).

### API walk-through

```bash
ALICE=$(gsm dev-token --principal alice); BOB=$(gsm dev-token --principal bob); CAROL=$(gsm dev-token --principal carol)

# Alice records a project decision (structured / oracle ingestion)
curl -s localhost:8080/v1/events -H "X-GSM-Context: $ALICE" -H 'content-type: application/json' -d '{
  "event_type": "decision.recorded",
  "operations": [{"op": "ADD_FACT", "fact": {
    "semantic_type": "decision", "content": {"text": "Launch Apollo on October 14"}, "topic": "Apollo launch",
    "attribute": "launch_plan", "subject_ids": ["project:apollo"],
    "policy_template": {"name": "project", "project_id": "apollo"}}}]}'

curl -s localhost:8080/v1/query -H "X-GSM-Context: $BOB"   -H 'content-type: application/json' -d '{"query":"Apollo launch"}'  # ANSWER
curl -s localhost:8080/v1/query -H "X-GSM-Context: $CAROL" -H 'content-type: application/json' -d '{"query":"Apollo launch"}'  # NO_MEMORY

curl -s localhost:8080/v1/forget -H "X-GSM-Context: $ALICE" -H 'content-type: application/json' -d '{"memory_ids":["<memory_id>"]}'
curl -s localhost:8080/v1/receipts/<receipt_id> -H "X-GSM-Context: $ALICE"                      # content-free receipt
```

Agent routes: `POST /v1/events`, `/v1/events/interpret` (dry run), `/v1/events/ingest` (model-interpreted),
`/v1/operations`, `/v1/query`, `/v1/search` (governed retrieval, no generation), `GET /v1/memories/{id}`, `/v1/memories/{id}/history`, `POST /v1/memories/update`,
`/v1/memories/supersede`, `/v1/memories/retire`, `/v1/memories/restrict`, `/v1/memories/derive`, `/v1/grants`, `DELETE /v1/grants/{id}`, `/v1/relationships`,
`POST /v1/forget`, `GET /v1/receipts/{id}`, `/v1/cascades/{id}`. Admin routes (header `X-GSM-Admin-Key`, disabled
when no key is configured) live under `/admin`. Health: `/healthz`, `/readyz`.

## Tests and checks

```bash
export GSM_TEST_DATABASE_URL=postgresql+psycopg://gsm_owner@localhost:55432/gsm_test   # the tests WIPE this database
pytest                  # 197 tests: unit, integration, adversarial, e2e acceptance, GateMem adapter
ruff check src tests migrations && ruff format --check src tests migrations
mypy                    # strict
```

Without `GSM_TEST_DATABASE_URL`, database tests are skipped and only the pure unit tests run.

## GateMem

```bash
# heuristic interpreter (offline)
gsm gatemem run   --data-dir ../bench/data/office --out outputs/gatemem/office --resolve-deletions lexical
# model-backed interpreter (any OpenAI-compatible server, e.g. a local vLLM Qwen3.8-27B)
GSM_LLM_BASE_URL=http://localhost:8003/v1 GSM_LLM_MODEL=Qwen/Qwen3.8-27B GSM_LLM_API_KEY=... \
gsm gatemem run   --data-dir ../bench/data/office --out outputs/gatemem/qwen_office --interpreter qwen \
                  --interp-cache outputs/gatemem/cache/qwen_interp.jsonl --workers 17
gsm gatemem score --data-dir ../bench/data/office --run-dir outputs/gatemem/qwen_office
```

See [docs/GATEMEM.md](docs/GATEMEM.md): the method never sees scoring-only fields; official metrics (U, A, F, OR,
MGS) come from the repository's own scorer, and it describes judging with a local Qwen3.8-27B.

## Documentation

* [ARCHITECTURE](docs/ARCHITECTURE.md): trust boundary, why the model is not the authority, request /
  ingestion / retrieval / disclosure / release / deletion flows.
* [POLICY](docs/POLICY.md): policy model, decision precedence, reason codes, adding a rule.
* [TEMPORAL](docs/TEMPORAL.md): truth / disclosure / retention states, semantic identity, supersession vs
  deletion, provenance and the deletion cascade.
* [RETRIEVAL](docs/RETRIEVAL.md): PostgreSQL pre-filtering, hybrid ranking, disclosure views, REFUSE vs
  NO_MEMORY, existence-leak prevention, adding a view.
* [PROVIDERS](docs/PROVIDERS.md): configuring model providers.
* [SECURITY](docs/SECURITY.md): invariant-to-enforcement map, assumptions, behavioral vs physical forgetting,
  limitations and future work.
* [GATEMEM](docs/GATEMEM.md): evaluation adapter.

## Layout

```
src/gsm/
  domain/        types, reason codes, PrincipalContext, policy model, operations, content
  policy/        engine (precedence), facts (metadata-only inputs), tags (SQL pre-filter), lineage, templates
  db/            Database / Tx (tenant-scoped SQL, RLS setting, app role)
  memory/        repo (SQL), writes (validate/triage/authorize/apply), forget, cascade, provenance
  retrieval/     planner, candidates (pre-filter + engine), search (hybrid ranking), evidence
  disclosure/    schemas (projections), planner (DisclosurePlan)
  release/       deterministic generator, release verifier
  providers/     interfaces, deterministic fakes, OpenAI-compatible adapters, factory
  audit/         content-free recorder, redacting JSON logging
  api/           FastAPI app (agent + admin routers)
  eval/gatemem/  sanitize (method side), adapter, scoring (only reader of hidden labels)
  service.py     GovernedMemoryService: the only entry point
migrations/      Alembic (hand-written SQL: schema, triggers, RLS)
tests/           unit/, integration/, e2e/, gatemem/
```
