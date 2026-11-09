# How EvolvingPrivacyMem works

This guide follows one conversation turn from arrival to answer, then lists every module.
It describes the code as it is in this repository: the core of the medical run plus the
domain hosts added afterwards (see [CHANGES_SINCE_MEDICAL_RUN.md](CHANGES_SINCE_MEDICAL_RUN.md)).
Older design reasoning is kept in [design/](design/).

## The idea in one paragraph

Several people and agents talk to one shared assistant. Each turn may contain facts that
belong to someone, such as a patient's medication, a project's staging token or a
resident's door code. Each turn may also contain permission events, such as "send my
results to my sister", "remove the old address" or "Dr. Shah is covering this weekend".
The memory stores every fact once, as a source-anchored record. For each record it decides
**whose information it is** (attribution) and **who may read it**. A reader gets access
through one of three routes: they own the record, they were shown doing work for its owner
(role-limited care access), or they hold an explicit grant from someone with authority.
Every retrieval, answer, revocation and deletion goes back through those decisions, so a
model's answer can only use what the asker may see.

## A turn, end to end

```
GateMem episode turn
  │  evaluation/gatemem_medical/input_boundary.py   only text, speaker ID, timestamp, kind (and reply_to) cross;
  │                                                 relationships, access levels and annotations stay behind
  ▼
InferenceFixtureAdapter.ingest_batch               (inference_adapter.py) builds the host bootstrap once per episode:
  │                                                 directory of people/entities, worker roles, role tables, conventions
  ▼
PermissionUpdater.ingest → update                  (agentic_memory/privacy/permissions.py, incremental.py)
  │   one combined model call per turn (incremental_wire.py: compact JSON schema)
  │     ├─ fact candidates: exact quoted spans, kind, information types   (fact_candidates.py)
  │     ├─ attribution: whose information, cited source turn              (attribution_v19.py)
  │     ├─ care labels: which workers are shown working for the subject   (care_v19.py)
  │     └─ control ops: grants, restrictions, revocations, erasure        (incremental.py, revocation_v19.py, lifecycle.py)
  │   host code then verifies every claim against the source text before it can matter
  ▼
compile                                            access edges and assignments in memory.privacy.sqlite
  │   owners (OWNS policy), care grants limited by role table, explicit grants, learned access cells,
  │   holds for uncertain controls (scoped_holds_v19.py, control_holds.py)
  ▼
PrivacyMemorySystem                                (system.py, storage.py) two SQLite stores:
                                                    memory.sqlite (records, provenance, tombstones)
                                                    memory.privacy.sqlite (policies, assignments, access edges, vectors)

Checkpoint question (asker ID + question only)
  ▼
answer_request                                     (answering.py) retrieves ONLY evidence the asker may read
  ▼
answer_validation_v19.answer                       one generation, one independent verification pass;
  │                                                 every claim must cite authorized evidence; removal_guard
  │                                                 returns no_memory for erased values; compares_subjects
  │                                                 refuses look-alike-subject comparisons without a model call
  ▼
prediction {action: answer | answer_redacted | refuse | no_memory, answer, retrieved_memory, ...}
  ▼
GateMemScorer                                      (scoring.py) the benchmark's rules + judge model → scores.jsonl
```

Evolution (A-MEM's linking and enrichment of related notes) runs after each boundary's
answers are committed, in durable batches of four (`evolution_queue.py`, `evolution.py`).
It can never add authority: semantic edges are protected resources too.

## Who may read a fact

The checks are applied in this order, and the last word always belongs to host code.

1. **Ownership (OWNS, V19).** A person reads facts attributed to them, across tasks. A host
   lists which directory roles are *subjects* (`Host.subject_roles`). When a fact names
   several people, only subjects own it (Stage 2 `information_owners`). A patient's
   relative named in a pickup note is mentioned, not an owner.
2. **Care access** (`care_v19.py`). A worker who is the speaker or is named in a turn, and
   whom the model labels as doing work for the subject, gets a grant on that subject.
   The grant covers only the information types in their role's row (`role_information_types`).
   A fact is readable only if *all* its types are in the row. Asking for information,
   claiming a role or working for a different subject does not count.
   Time-limited workers lose access 24 hours after their last involvement.
3. **Learned access** (`care_v19.learned_access`, new in this system). A role's row also
   opens a type once workers of that role, each shown working for the fact's subject,
   wrote facts of that type in 2 different source turns (`--learned-access-sources 2`).
   Cells only open. **Reply access** does the same for one person when a turn carries
   `reply_to`, but no GateMem turn has `reply_to`, so it is inactive on this benchmark.
4. **Explicit grants** from a subject, or, for subjects that never speak (projects,
   programs), from the host's authority roles (`subject_authority_roles`). The grant covers
   only the fields it names.
5. **Restrictions, revocation, expiry and erasure** apply on top of all of the above.
   Erasure removes exact source-derived values (`lifecycle.py`, `incremental.erasure_selectors`).
   Its scope fix keeps unrelated facts that share no distinctive word with the named values.

## Where the domain knowledge lives

Nothing in `agentic_memory/` knows about hospitals, offices, schools or homes. A domain is
a `Host` in `evaluation/gatemem_medical/hosts.py`:

| Field | Meaning |
|---|---|
| `subject_roles` | Directory roles a record can belong to: `patient`; `project`; `student`, `program`, `project`; the resident roles |
| `entity_kinds` | Non-person subjects, read only from `<kind>_id` values in the episode's relationship rows (the rows themselves never cross the boundary) |
| `care_scopes`, `information_types`, `role_information_types` | The role tables. For this system they come from a generated policy (`hosts.from_policy`), not from `hosts.py` |
| `subject_authority_roles` | Who controls subjects that never speak. Comes from the generated policy's authority vote |
| `temporary_care_hours` | 24 everywhere |
| `task_convention`, `identity_convention` | Host wording the model sees about who owns what |
| `requires_policy` | Education and household have no hand-written tables, so the runner refuses them without `--role-policy` |

[HOSTS.md](HOSTS.md) describes all four hosts and the reasons behind them.

## Generated role policies (`scripts/build_role_policy.py`)

The builder's inputs, and nothing else:

- The job titles, read from `entities.principals[].role`, minus the assistant, the person
  subjects and people who are only ever mentioned (`hosts.policy_excluded_roles`).
- The benchmark's published policy paragraph (`bench.domains.get_query_policy_block`),
  which GateMem gives every tested system.

**Step 1** is one call at temperature 0, seed 0. The model describes each job's work and
defines 6–12 kinds of information, named by what the information is about.

**Step 2** is five votes at temperature 0.6, seeds 1–5. Each vote says which kinds each job
needs, as least privilege. For hosts with non-speaking subjects, the votes also say which
jobs control them. A cell opens with 3 of 5 votes.

Every prompt, schema and reply is saved in `provenance.json`. `pipeline/freeze_policy.py`
checks a policy and freezes it into a run, with a SHA-256. The run's manifest records
`policy-sha256:<hash>`, and `smoke_test.sh` proves that the harness loaded exactly that
file.

## Module map

### `agentic_memory/privacy/` (the governed runtime)

| Module | Role |
|---|---|
| `system.py` | `PrivacyMemorySystem`: canonical notes, deterministic authority checks, protected graph, sessions, `retrieve` |
| `storage.py` | The two SQLite stores and their schema; super-journal commits across both |
| `permissions.py` | `PermissionBootstrap` (host configuration), `PermissionUpdater` (per-turn interpretation, repair, compile), `PermissionSourceEvent` |
| `incremental.py`, `incremental_wire.py`, `incremental_policy.py`, `incremental_routing.py`, `incremental_v17.py`, `incremental_v18.py` | The combined per-turn model call: compact schema, expansion, interpretation rules, control kinds, erasure selectors |
| `fact_candidates.py` | Extraction of exact, source-addressable fact spans; requests are not stored as facts |
| `attribution_v19.py` | Whose information a fact is; citation checks, rival subjects, `information_owners` |
| `care_v19.py` | Care labels → role-limited grants; temporary workers; learned and reply access |
| `ownership.py` | Host-configured personal ownership (OWNS) |
| `revocation_v19.py`, `scoped_holds_v19.py`, `control_holds.py` | Recipient and type revocation; fail-closed holds when a control is uncertain |
| `lifecycle.py`, `scheduled_forgetting.py` | Erasure of exact values, deferred erasure |
| `privacy_fallback_v19.py` | Bounded privacy assessment when a turn's interpretation fails |
| `acceptance_v19.py`, `task_contracts_v19.py`, `task_tracking.py`, `host_tasks_v19.py` | Task-based access: acceptance, trusted dependency contracts, completeness, host-issued tasks |
| `answering.py` | `PermissionQueryService`, `answer_request`: authorized retrieval and output contract |
| `answer_validation_v19.py` | Answer certificates: generation, independent verification, repair, removal guard |
| `evolution.py` | Source-grounded A-MEM evolution that never publishes a partial job |
| `processors.py` | Model and embedding processors (OpenAI-style endpoints, local MiniLM, offline doubles) |
| `governance.py`, `learning.py`, `permission_context.py`, `permission_state.py`, `permission_wire.py`, `simple_permissions.py`, `quote_selection.py`, `ingestion.py`, `migrate.py` | Supporting pieces: capability approvals, context windows, compact deltas, earlier single-pass permissions, quote choice, source envelopes, store migration |

### `agentic_memory/` (original A-MEM)

`memory_system.py` (`AgenticMemorySystem`), `retrievers.py`, `llm_controller.py`: the
unguarded upstream memory. The privacy runtime does not use it for GateMem.

### `evaluation/gatemem_medical/` (the benchmark harness, all four domains)

| Module | Role |
|---|---|
| `runner.py` | `python -m evaluation.gatemem_medical`: pinned dataset, boundary schedule, journaled resumable run, manifests, scoring |
| `input_boundary.py` | What may cross from GateMem fixtures to the agent |
| `hosts.py` | The four domain hosts and `from_policy` |
| `inference_adapter.py` | Builds the bootstrap from a host and an episode; ingests turns; subject authorities |
| `adapter.py` | Data loading, schedule, the metadata-assisted medical baseline |
| `scoring.py`, `rejudge.py`, `openrouter.py` | Benchmark scoring with a judge model; independent OpenRouter rejudge |
| `recovery.py`, `operational.py`, `evolution_queue.py`, `lifecycle.py`, `access_audit.py`, `evolution_ablation.py` | Retry and recovery, operational limits, evolution batches, ablations and audits |
| `AUTHORITY_POLICY.md`, `RUNBOOK.md` | Historical harness notes |

### `pipeline/` (running a domain)

See [RUNNING.md](RUNNING.md). Each script's header says exactly what it does.

## Tests

| Suite | What it covers |
|---|---|
| `evaluation/gatemem_medical/tests` | Harness, input boundary, office host, education and household hosts (scripted labels), role policies, policy builder prompt identity, scoring |
| `pipeline/tests` | Reproduces the medical summary, policy freeze, strict rejudge (replaying saved GPT-4.1 replies) and the combined rejudge source byte for byte; checks export manifests |
| `security_tests` | The runtime's privacy behaviour: ownership, attribution, care, revocation, erasure, answer validation, holds, migration |
| `tests` | The original A-MEM (needs `nltk` and `chromadb`) |
