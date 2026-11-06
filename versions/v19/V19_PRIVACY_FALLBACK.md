# V19: bounded independent privacy assessment after failed processing

Workspace `/home/thaddus/a-mem-v19-fixes`, branch `v19-recipient-revocation`, base
`02a7c8f` plus the earlier uncommitted context fixes (see
`analysis/v19-context-fixes/README.md`, whose runtime hashes were verified before
this change). Implementation: `agentic_memory/privacy/privacy_fallback_v19.py`.

## Policy (agreed; deliberately not fail-closed)

A processing failure alone never permanently withdraws established access. When
ordinary processing exhausts its existing repair budget, `skip_exhausted` runs a
bounded finalization **before** terminal quarantine:

1. If a valid independent assessment already exists, no extra call is made:
   the complete-message medication grammar (`VALIDATED_NON_CONTROL`), a validated
   ordinary no-change control review (`CONTROL_VALIDATED_NO_CHANGE`), or a complete
   local adverse boundary whose applicability needs no further decision.
2. Otherwise at most **two extra model calls** for that source event: a focused
   privacy assessment of the complete original message, then a correction call
   only if the first response was technically failed, unusable, stale, or had
   clauses/applicability that failed host validation. Explicit `unresolved` is a
   valid answer and is not re-asked.

| Outcome | Meaning | Effect |
| --- | --- | --- |
| `NO_PRIVACY_INSTRUCTION` | The complete event contains no privacy instruction. | Event uncertainty released. |
| `SUPPORTED_CHANGES` | One or more independently validated adverse clauses (local compiler and/or model). | Published through existing validators; unresolved remainder released. |
| `UNRESOLVED_RELEASED` | Meaning, recipient, scope, timing, authority or applicability unresolved. | No hold for the unresolved portion; established permissions remain. |
| `TECHNICAL_EXHAUSTION` | Both extra calls failed technically or were unusable. | Uncertainty released after budget exhaustion. |

A successful ordinary publication finalizes as `CONTROL_COMMITTED`; a partial
local adverse signal on such an event is recorded as released, not held.

Releasing uncertainty never removes validated controls (from this or another
event), never cancels restrictions, revocations, expiry or erasure, and never
creates grants, bindings, approvals or task authority. `_validate(...,
independent_adverse=True)` accepts only `record_restriction`, `erase_information`
and complete Batch 1 `revoke_assignment` operations, skips ownership grant
compilation, and keeps every other authority/evidence check.

## Separate ledgers

| Concern | Record |
| --- | --- |
| Processing status and repair budget | `permission_jobs`, `permission_attempts` (unchanged; failures preserved) |
| Temporary uncertainty | `control_assessments_v19` rows in `UNASSESSED`/`POSSIBLE_ADVERSE`, enforced **only** while the job is active and not finalized; an assessment interrupted mid-call stops protecting |
| Extra-call attempts | `privacy_fallback_attempts_v19`: persisted as `dispatched` before each call; statuses `validated`, `failed`, `unusable`, `stale`, `interrupted`; prompt/policy version, request digest, snapshot, source digest, response, per-clause validation, published controls, usage, latency |
| Validated controls | Existing ledgers: restrictions in `permission_records` (marked `independent_privacy_v19` with citation, identity receipts, validator), erasure via `apply_erasure`, revocations via Batch 1 |
| Applicability | `privacy_scope_reviews_v19`: complete-fact receipts (exact full fact quote, fact and source digests, restriction, attempt) |
| Finalization | `finalization` on the assessment row and `privacy_assessment` on the job audit: outcome, assessed_by, verified controls, deletions, unresolved portions, release reason, extra calls |

## Grounding and scope

- Clause quotes must be exact substrings of the original event; only immediate
  timing is applied; the speaker must hold authenticated restriction authority.
- Recipients are literal references inside the clause, resolved through
  authenticated self introductions/relationships (`revocation_v19`), never
  roles or model-selected IDs. Self introductions such as "I am his sister
  Nina" now name the speaker (not the relationship).
- A deny scope is an exact phrase of the clause or `*`; erase values must be
  named verbatim in the deletion clause. Model clauses equivalent to a validated
  local control only contribute applicability.
- Existing approved facts: a restriction applies when the sound closed
  complete-fact grammar proves the field, or a fresh durable receipt says
  `applies`. Unestablished applicability is **not** speculatively blocked.
  Receipts are invalidated by a changed complete fact or source.
- Later facts/grants: ordinary complete-fact binding reviews see the restriction
  (`blocked_by`); publishing an independent restriction does not invalidate
  earlier approvals (only superseding it does).
- Deletion runs the existing erasure path (fingerprints, candidates, views and
  descendants, resurrection prevention); unresolved deletions are recorded as
  unresolved, never as completed.
- No model call during reads; queries, restarts and later turns never reassess
  a finalized event or reset its budget. Model calls run outside transactions;
  publication rechecks policy snapshot and source digests, and the core
  rechecks every input after each call.

## Versions and compatibility

Source prompt `permission-inference-v19-independent-privacy-v1`; assessment
prompt `privacy-assessment-v19-independent-v1`; policy
`v19-independent-privacy-fallback:1`; scoped-hold policy `v19-scoped-control-holds:2`.
A V19 store with V19 jobs but no current policy marker (or another marker) is
refused before any installer mutates it. Historical databases, predictions,
scores and exhausted budgets are not migrated, replayed or rewritten; a newer
reader of a historical store keeps the unchanged Batch 4 predicate.

Metrics: `processing_metrics()['independent_privacy_v19']` (outcomes, assessed_by,
extra calls/statuses/latency, verified controls, applicability receipts, active
temporary holds). Host audit: `PermissionUpdater.privacy_assessments()`.
`skip_exhausted` returns a `privacy_assessment` summary, which the GateMem
adapter writes to the run log and `recovery-events.jsonl` (`unresolved_access_hold`
reflects the actual finalization). The benchmark answer schema is unchanged.

## Verification and limitations

See `analysis/v19-privacy-fallback/README.md` for the exact commands, test
counts and the 004-01 card replay. All of those results use scripted test
doubles; they are not real-model results.

Known limitations:

- Model quality of the extra assessment (clause recall, scope phrases,
  applicability) is unmeasured; only host validation is tested.
- Applicability that is never established (both calls failed, or a fact beyond
  the 48 offered) leaves a validated scoped restriction unenforced on that
  existing fact — the agreed policy, not a fail-closed fallback.
- The pre-existing Batch 5 attribution guard withholds facts naming unknown
  proper names (e.g. "Cedar Bridge Recovery House", "Darius", "Suite 4C" in
  episode 002) from everyone, independently of this change. It limits
  002-06/002-09 utility and makes the 002 deleted details unreadable even before
  deletion.
- An in-process active marker scopes temporary protection; a separate reader
  process treats an in-flight assessment as interrupted (released), which errs
  toward the agreed release policy.
