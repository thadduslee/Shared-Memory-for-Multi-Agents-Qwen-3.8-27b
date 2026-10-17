# Security invariants, assumptions and limitations

GSM is a research MVP. It makes **no claim** of formal security, regulatory compliance or cryptographic erasure.
What it does claim is below, together with where each property is enforced and which tests exercise it.

## Invariants → enforcement → tests

| # | Invariant | Enforcement | Tests |
|---|---|---|---|
| 1 | Identity, roles, auth strength, tenant and purpose come from trusted context | `PrincipalContext` (frozen, strict, trusted issuers); authenticators resolve roles from DB bindings, tokens can only narrow them | `integration/test_tenant_isolation_and_identity.py::TestTrustedIdentity`, `unit/test_context_and_config.py`, `integration/test_api.py::test_token_roles_can_only_narrow` |
| 2 | Agents and model adapters have no DB access | Providers receive only method arguments; only `GovernedMemoryService` holds a `Database` | by construction; `unit/test_openai_compat.py::test_generator_receives_only_projections` |
| 3 | All reads/writes go through the governed service | API routes call only the service; admin routes are separate | `integration/test_api.py` |
| 4 | Unauthorized records never enter the generator's context | Stage A/B pre-filter; generator gets only projections of authorized items | `integration/test_retrieval_and_disclosure.py::test_unauthorized_never_in_candidates_bundle_or_generator` |
| 5 | Authorization happens before ranking | Ranking joins only engine-authorized `(version, view)` pairs; exact similarity inside that set | `...::test_vector_ranking_is_confined_to_authorized_subset`, `...::test_redacted_ranking_ignores_hidden_fields` |
| 6 | Denial, revocation, expiry, tombstoning and tenant mismatch override grants | Engine precedence stages 1-4 before allow paths | `unit/test_policy_engine.py::TestPrecedence`, `integration/test_access_control.py::TestGrantsAndDenials` |
| 7 | Derived memories inherit parent restrictions | Live minimum over parent decisions; conservative meet stored; declassification only by owners of all parents via oracle | `integration/test_write_governance.py::TestDerivation`, `unit/test_policy_engine.py::TestHistoryAndLineage` |
| 8 | Tombstones make content immediately unretrievable | Sync transaction tombstones versions + atom and quarantines descendants; every read path requires active state | `integration/test_forgetting.py` (incl. deferred cascade), race tests |
| 9 | Audit never reproduces protected or deleted content | Recorder writes ids, decisions, reason codes, counts, keyed query fingerprints; logs redact sensitive keys and exception messages | `integration/test_audit_and_release.py::test_audit_records_are_content_free`, `unit/test_schemas_logging_tags.py::TestLogging` |
| 10 | Failures fail closed | Malformed policy ⇒ DENY; missing/untrusted context ⇒ 401; unknown ops ⇒ schema rejection; provider failure ⇒ deterministic fallback / no proposal; no authenticator configured ⇒ no access | `unit/test_policy_engine.py::test_malformed_policy_fails_closed`, `integration/test_api.py::test_no_authenticator_means_no_access`, `unit/test_openai_compat.py` |
| 11 | Model output never directly commits a high-impact change | Triage quarantines model deletes/grants/revokes/relationship changes, re-classification, authored policies/owners | `integration/test_write_governance.py::TestQuarantine` |
| 12 | Decisions carry stable reason codes and a policy version | `Reason` enum (append-only); `policy_version = engine#revision` | `unit/test_policy_engine.py::test_decisions_carry_version_and_ids` |
| 13 | Queries do not confirm protected existence | See [RETRIEVAL.md](RETRIEVAL.md#refuse-versus-no_memory-and-existence-leakage) | `...::test_hidden_existence_is_indistinguishable_from_nothing`, `test_tenant_isolation_and_identity.py::test_cross_tenant_id_is_indistinguishable_from_missing` |
| 14 | Tenant ids are enforced on every path | `Tx` injects `:tenant_id` and refuses SQL without it; composite `(tenant_id, …)` FKs; forced RLS under `gsm_app` | `...::test_row_level_security_blocks_cross_tenant_sql`, `...::test_tx_refuses_unscoped_sql_and_injected_tenant` |
| 15 | Reindexing never restores deleted or superseded state | Reindex reads only non-tombstoned, unscrubbed versions; triggers reject index rows or new versions for tombstoned memories and un-tombstoning | `integration/test_forgetting.py::test_reindex_cannot_restore_deleted_content`, `::test_db_refuses_new_versions_on_tombstoned_memory` |

The SQL linter rule `S608` is disabled because statements are assembled only from fixed constant fragments
(derivation edge list, version predicates). Every value is a bound parameter, and the single interpolated
identifier (`SET ROLE`) is regex-validated.

## Security-sensitive assumptions

* **The authenticator is trustworthy.** The HMAC dev authenticator is for development. Production needs a real
  identity provider; with none configured, every agent route returns 401.
* **The service process and database owner are trusted.** RLS is defense in depth against application bugs,
  not against a compromised service. The migration role and DB superusers bypass RLS.
* **Policy and schema authors are trusted.** In particular, `topic` labels are disclosed in existence-only
  views. The service rejects topics that repeat categorized field values, but it cannot judge free text.
* **Clocks are sane.** Timestamps come from the service clock, not from requests.
* **Secrets stay secret.** The admin key, dev auth secret and audit HMAC key come from the environment.
  Production refuses to start without an audit key.
* **Ownership is namespaced.** Semantic identity is scoped by owner set, so writes never reveal others'
  records. As a trade-off, two owners may hold parallel claims.

## Behavioral forgetting, not certified erasure

After `forget` commits, no GSM read path returns the content. After the cascade, the plaintext is gone from
every live row the service owns: version content, projections/tsvectors, embeddings, topics, and raw event
payloads. `tests/integration/test_forgetting.py::test_deleted_plaintext_is_gone_from_the_whole_database`
checks every text/jsonb/array/tsvector column. This is **behavioral** forgetting. It does not cover:

* **PostgreSQL internals:** dead tuples from MVCC until `VACUUM` reclaims them (and even then, until the pages
  are overwritten), WAL segments, replication slots, replicas, and PITR base backups and archives.
* **Everything outside the database:** OS page caches, swap, filesystem snapshots, and disk remapping.
* **External systems:** model providers' logs and embedding-provider caches, for text they were shown before
  deletion. This matters only when model-backed providers are configured, and only for the *permitted
  projections* they saw.
* **Retained metadata:** identifiers, types, subjects, owners and timestamps are retained by design, for audit.

Certified erasure would need, at minimum:

* per-memory data keys (envelope encryption) destroyed at tombstone time, i.e. crypto-shredding;
* WAL and backup retention aligned with deletion SLAs;
* provider-side deletion guarantees.

None of this is implemented.

## Known limitations

* **Side channels.** Response latency is not constant, and aggregate views over very small groups (e.g. one
  person) can identify individuals. There is no minimum-group-size rule yet.
* **Model generators.** They are checked by deterministic heuristics: approved-id citations, keyed exact-value
  fingerprints (values under 4 characters are not fingerprinted), and category lexicons. A model cannot reveal
  hidden fields it never received, but it can still draw inferences from permitted content.
* **Interpretation quality.** Natural-language interpretation limits end-to-end quality (see
  [GATEMEM.md](GATEMEM.md)). GSM enforces the policies it is given; it does not infer intent from dialogue.
* **Process-local cache.** Correctness does not depend on invalidation, because every hit is re-verified
  against the database. Recall of *new* memories may lag by up to the TTL in other processes.
* **The GateMem model interpreter is an evaluation harness, not a production ingestion path.**
  * *What it trusts.* It treats each speaker's own words as that speaker's authenticated sharing
    instruction, and it acts as the confirming human for deletions the speaker requests.
  * *Where its judgment stays.* GSM still authorizes every write and deletion. Audiences are limited to a
    closed per-episode menu, and a floor removes "everyone" from sensitive turns. But choosing *which* group
    or role is still the model's judgment, and a wrong choice is a wrong policy.
  * *What its context can carry.* It sees standing instructions and the three previous turns, so content from
    context can flow into a model-written statement, summary or topic.
  * *Quotes are model-chosen.* The stored full view is a quote the model selected, verified to occur in the
    message; a quote can still omit context (e.g. a pronoun's referent). A completeness guard keeps the whole
    turn too when a value was dropped. Masking of retired values after a correction is exact-string based
    (plus normalized detection, which drops the quote) and covers values recorded on the deleted memories.
  * *Subjects and exclusions.* Owners (whose own information it is) always see it; people merely mentioned
    see it only through the audience the model chose. Who counts as an owner is the model's call. A message that says not to tell someone relies on the model reporting
    that exclusion; when it does, an explicit denial enforces it.
  * *Re-scoping trusts the speaker's words like everything else.* A consent or revocation is applied as the
    speaker, and GSM authorizes it: restrictions need update authority, grants need owner or subject
    authority. Which earlier memories an instruction covers is the model's choice among what the speaker can
    see, so a too-broad choice over-restricts (utility loss) and a too-narrow one leaves access in place.
    Restrictions resolve groups to their members at the time of the instruction; later members are not
    covered.
* **Query decomposition** sends the requester's own question, and nothing else, to the planner model. A bad
  decomposition can lower recall but cannot widen access: parts are ranked only over authorized pairs.
* **Scope gaps:**
  * valid-time only (no transaction-time axis);
  * no persisted quarantine queue (quarantined proposals are returned to the caller);
  * no per-tenant engine configuration;
  * no OIDC/JWT authenticator;
  * single database.
* **Search cost.** Exact vector similarity over large authorized sets is O(n). pgvector 0.8 iterative index
  scans are a possible optimization. Recall is never traded for leakage.
* **Remaining existence channel.** Relationship removal returns `NOT_AUTHORIZED_TO_WRITE` rather than
  `NOT_FOUND` for relationship ids a caller cannot manage. The ids are random and relationships are directory
  data, but this is a residual existence channel.

## Future work (highest value first)

1. **Production identity:** OIDC/JWT authenticator with signed purpose claims, per-agent workload identity,
   step-up flows for `ESCALATE`, per-tenant engine configuration, and audited policy changes.
2. **Governed interpretation:** model-backed extraction into typed atoms, with target resolution for
   natural-language deletions/grants and a persisted, encrypted confirmation queue for quarantined proposals.
   This is what GateMem utility and forgetting now depend on.
3. **Crypto-shredding and storage hygiene:** per-memory envelope keys destroyed at tombstone time, scheduled
   `VACUUM`, and WAL/backup retention aligned with deletion SLAs.

After those:

* recomputation of derived memories from surviving parents;
* minimum group sizes for aggregates;
* a shared cache generation counter;
* a bitemporal model;
* iterative pgvector index scans.
