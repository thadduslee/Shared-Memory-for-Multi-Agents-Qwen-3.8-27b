# Temporal state, provenance and forgetting

## Three separate dimensions

| Dimension | Where | Values |
|---|---|---|
| **Truth**: is this the current account of the fact? | `memory_versions.truth_state` | `current`, `contradicted`, `stale`, `superseded` |
| **Disclosure**: who may receive which view? | policy + engine decisions | `full`, `redacted`, `aggregate`, `existence_only`, or nothing |
| **Retention**: may the content exist at all? | `memory_atoms.lifecycle_state`, `retain_until`; `memory_versions.state` | atom: `active`, `expired`, `pending_invalidation`, `tombstoned`; version: `active`, `superseded`, `expired`, `tombstoned` |

A superseded version is *not current* but still *retained*, so authorized historical queries can read it.
A tombstoned version is not retained: no query, current or historical, can reach it.

## Immutable versions

`memory_versions` rows are never rewritten. A `BEFORE UPDATE` trigger rejects any change to content, identity,
`valid_from` or supersession links. The only content change it allows is scrubbing to `NULL`, and only once the
version is tombstoned. Tombstoning is terminal: triggers reject un-tombstoning an atom or a version, inserting a
new version into a tombstoned atom, and writing index material (projections or embeddings) for tombstoned
versions.

* `UPDATE_FACT` locks the atom and its active version. It checks `expected_version_id` (optimistic
  concurrency), marks the old version `superseded` (or `contradicted` when `contradiction=true`) with
  `valid_until = new.valid_from`, and inserts the new version with `supersedes_version_id` set.
* `RETIRE_FACT` ends the current version's validity without a replacement (state `superseded`,
  `valid_until = now`). It is authorized like an update and needs no policy check, because it only narrows:
  current queries stop returning the value, and authorized historical queries still can. Use it when the new
  value comes from someone who may update the memory but shares with a different audience, so a supersession
  would widen access (`POLICY_BROADENING`). Model-proposed retirements are quarantined like deletions.
* A partial unique index allows **one active version per memory**. A second one on
  `(tenant, workspace, semantic_key)` allows **one active value per semantic identity**.
* Concurrency outcomes: writers with the same base version produce exactly one winner and `VERSION_CONFLICT`
  for the rest. Blind concurrent writers serialize into a single linear chain. Concurrent adds of the same
  identity produce one winner.

## Semantic identity

A fact is *single-valued* when its writer supplies an `attribute`. Its identity is:

```
semantic_key = semantic_type | sorted(subject_ids) | attribute | sorted(owner_ids)
```

Embedding similarity never decides supersession. The owner namespace is deliberate: a principal's write can
never collide with, and therefore never reveal, a record owned by someone else. The cost is that two owners
may hold parallel claims about the same subject/attribute. Facts without an attribute are multi-valued and
never collide.

## Validity, expiry and `as_of`

* `valid_from` / `valid_until` bound when a version is true. Current queries require
  `valid_from ≤ now < valid_until`.
* `retain_until` bounds how long the atom may be retained. Once past, it is unavailable even before any sweeper runs.
* `as_of` queries resolve the version valid at that time (`active`, `superseded` or `expired`, never
  `tombstoned`). Reading a non-current version is **historical** and requires `policy.historical_access`
  (default `owners_only`).
* The model is valid-time only. A bitemporal (transaction-time) axis is future work.

## Supersession versus deletion

| | Supersession | Deletion |
|---|---|---|
| Meaning | the fact changed | the information must be forgotten |
| Old content | retained, historically auditable | scrubbed |
| Current queries | return the replacement | return nothing |
| Historical queries | may return the old version (if authorized) | return nothing |
| Derived memories | unaffected | tombstoned by the cascade |
| Operation | `UPDATE_FACT` / `SUPERSEDE_FACT` (new atom + `supersedes` edge) | `DELETE_FACT` / `forget` |

## Provenance

`memory_edges` are typed and point from parent to child: `source_event` (raw event → memory), `derived_from`,
`summary_of`, `report_of`, `supersedes`, `contradicts`. Only the three derivation types are followed by the
deletion cascade. Supersession and contradiction are not derivation.

### Derived memories and inherited policy

`derive(parent_ids, content, ...)` requires a FULL view of every parent. The derived memory stores a
conservative meet of its parents:

* sensitivity is the maximum;
* compartments are the union;
* allowed views are the intersection (empty ⇒ reject);
* mandatory purposes are the intersection of the non-empty sets (conflict ⇒ reject);
* auth strength is the maximum.

It is then **governed live by its parents**: each read is the minimum view over fresh decisions on all
parents. A parent that is revoked, denied or deleted therefore hides the summary immediately, before the
cascade even runs. Explicit denials and mandatory constraints on the derived memory itself still apply.

Only an owner of every parent, through the oracle path, may attach a `declassification` policy that replaces
inheritance. Models cannot.

## Deletion

### Synchronous transaction (`forget`)

1. Authenticate; resolve **exact** ids. Selector-based deletes require `expected_count` when more than one
   memory matches, otherwise `AMBIGUOUS_TARGET`.
2. Lock the atoms. Authorize each one. Invisible targets return `NOT_FOUND`; visible but not deletable return
   `NOT_AUTHORIZED_TO_WRITE`. The whole request fails if any target fails.
3. Tombstone every version and the atom; write content-free `deletion_tombstones` rows.
4. Move every derivation descendant to `pending_invalidation`, so it is unavailable at once.
5. Create one `deletion_cascades` job per tombstone, plus a content-free receipt and policy decisions.
6. Commit. From this point every retrieval path excludes the memory, because they all require
   `lifecycle_state = 'active'` and a non-tombstoned version. Caches are invalidated. A cache hit elsewhere is
   rechecked against the database before it is served.

A repeated request by the original requester is a safe no-op. Anyone else gets `NOT_FOUND`.

### Cascade (`CascadeRunner`, inline by default, or `GSM_CASCADE_MODE=deferred`)

For each job (`FOR UPDATE SKIP LOCKED`): traverse descendants in depth order, and let a `DerivedRecomputer`
decide each one. The MVP `ConservativeRecomputer` applies the rule *any tombstoned parent ⇒ tombstone*. A
future recomputer can rebuild a derived memory from its remaining valid parents.

The cascade then:

* scrubs version content;
* deletes projections (lexical index material) and embeddings;
* blanks topic labels;
* revokes grants;
* scrubs the payload of source raw events;
* evicts caches;
* records counts only.

Every step is guarded by current state, so re-runs are no-ops. A failure rolls back and records only the
exception *type*. Raw event payloads that no committed memory references are scrubbed at ingestion time, so
text that no memory owns never outlives a deletion.

### What survives deletion

Survivors are identifiers, types, subjects, owners, timestamps, lifecycle states and counts. That is what the
receipts need to show *what was decided*. No content, content hash, query text or model output survives. See
[SECURITY.md](SECURITY.md) for why this is behavioral forgetting, not certified erasure.
