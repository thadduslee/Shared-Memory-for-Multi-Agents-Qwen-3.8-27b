# V19 bounded independent privacy assessment: validation

Workspace `/home/thaddus/a-mem-v19-fixes`, base `02a7c8f` plus uncommitted changes.
Design and policy: [`V19_PRIVACY_FALLBACK.md`](../../V19_PRIVACY_FALLBACK.md).

**Every result in this directory comes from scripted test doubles. None is a real-model
result and none is a benchmark score.** Source processing, extraction, binding
reviews, the extra privacy assessment and answers are scripted (`SemanticModel`,
per-test assessors, and the current answer-validation test model `EvidenceModel`).
They test host behaviour: validation, publication, release, enforcement and
audit. They do not measure how well the real model assesses privacy.

## What changed

- `privacy_fallback_v19.py` (new): after the existing two-call repair budget is
  exhausted, `skip_exhausted` finalizes the event's uncertainty before quarantine.
  No extra call is made when a valid assessment already exists (the complete-message
  grammar, a validated no-change control review, or a complete local adverse
  boundary). Otherwise there are at most two extra calls: an assessment of the
  complete original message, then a correction only if the first response failed
  or did not validate. Outcomes are `NO_PRIVACY_INSTRUCTION`, `SUPPORTED_CHANGES`,
  `UNRESOLVED_RELEASED` and `TECHNICAL_EXHAUSTION`; successful ordinary publication
  is recorded as `CONTROL_COMMITTED`.
- `scoped_holds_v19.py`: a temporary uncertainty hold applies only while its job is
  active and not finalized. Historical stores keep the unchanged Batch 4 predicate.
- `permissions.py`: refuses a store from a different policy (fresh-store marker);
  `_validate(..., independent_adverse=True)` accepts only restrictions, erasure and
  complete Batch 1 revocations, never grants. `permission_gate` enforces scoped
  independent restrictions on facts whose field is proven or that have a fresh
  `applies` receipt. `skip_exhausted` returns a privacy-assessment summary.
  `privacy_assessments()` is a host-only audit.
- `revocation_v19.py`: "I am his sister Nina" names the speaker, not the relation.
- `incremental.py`: binding-review context shows independent restrictions to later
  reviews without invalidating earlier approvals.
- `control_holds.py`: a finalized event creates no legacy control hold.
- `fact_candidates.py`: processing metrics include `independent_privacy_v19`.
- `evaluation/gatemem_medical/inference_adapter.py`, `recovery.py`: the run log and
  `recovery-events.jsonl` report the actual finalization outcome. Previously they
  always said "protective access hold retained". The answer schema is unchanged.

## Verified controls versus released instructions

| Kind | What it is | Where it is recorded |
| --- | --- | --- |
| Verified control | A clause whose exact quote, immediate timing, speaker authority, literal recipient identity and scope or values all passed host validation. It was published through the existing validators: a restriction, erasure through `apply_erasure`, or a Batch 1 revocation. | `permission_records` (`independent_privacy_v19` citation, identity evidence, validator), `source_erasures`, `finalization.verified_controls` / `deletions` |
| Released unresolved instruction | A possible instruction whose meaning, recipient, scope, timing, authority or applicability could not be established; or an event whose assessment calls failed technically. It creates **no** hold, no grant and no control. Established permissions stay as they were. | `finalization.unresolved`, `release_reason`, `outcome` (`UNRESOLVED_RELEASED` / `TECHNICAL_EXHAUSTION`), `privacy_fallback_attempts_v19` |

Releasing uncertainty never removes or weakens a verified control from any event.

## Commands and results

Python: `/home/thaddus/gatemem/.venv-nb/bin/python` (written `$PY` below).

```bash
cd /home/thaddus/a-mem-v19-fixes
PY=/home/thaddus/gatemem/.venv-nb/bin/python

# Broad regressions (V20 modules need an implementation absent from this checkout;
# the episode replays run separately because they take several minutes)
$PY -m pytest security_tests evaluation/gatemem_medical/tests -o addopts='' -q -p no:cacheprovider --tb=short \
  --ignore=security_tests/test_answering_v20.py --ignore=security_tests/test_workflow_v20.py \
  --ignore=security_tests/test_privacy_fallback_episodes_v19.py \
  --junitxml=analysis/v19-privacy-fallback/regression-tests.xml

# GateMem medical source-prefix replays (episodes 004 and 002)
$PY -m pytest security_tests/test_privacy_fallback_episodes_v19.py -o addopts='' -q -p no:cacheprovider --tb=short \
  --junitxml=analysis/v19-privacy-fallback/episode-replay-tests.xml

# 004-01 evidence-card replay (current answer-validation contract, scripted)
PYTHONPATH=. $PY analysis/v19-scoped-control-holds/replay_004_01.py \
  --card /home/thaddus/a-mem-variant/analysis/v19-v21-evidence-review-20261102/cards/v19/004-01.json \
  --output analysis/v19-privacy-fallback/004-01-card-replay.json

# Summary with runtime hashes
$PY analysis/v19-privacy-fallback/validation_summary.py
```

Results on the final code (`validation-summary.json` records the runtime hashes):

| Report | Result |
| --- | --- |
| `regression-tests.xml` (broad) | 886 passed, 1 known baseline failure; 48 subtests passed |
| of which `test_privacy_fallback_v19.py` | 36 passed |
| of which `test_scoped_holds_v19.py` / `test_recipient_revocation_v19.py` / `test_host_tasks_v19.py` | 40 / 40 / 35 passed |
| `episode-replay-tests.xml` (004 and 002 source prefixes) | 13 passed |
| `004-01-card-replay.json` | t039 finalizes `NO_PRIVACY_INSTRUCTION` with 0 extra calls; both approved plan recaps are released; all three required plan details are in the answer; 0 external model calls |

In the 004 replays, t037 and t049 finalize as `NO_PRIVACY_INSTRUCTION` after one
extra call, or as `TECHNICAL_EXHAUSTION` after two calls when both time out. t039
needs no call (complete-message grammar). In 002, t073 publishes a verified
Nina-only restriction and t157 a verified deletion through the existing erasure
path (one extra call each).

`acceptance_v19.py`, `answer_validation_v19.py`, `attribution_v19.py` and
`incremental_wire.py` appear in the runtime hashes because of the earlier,
already-validated context fixes (`../v19-context-fixes/`). This change does not
modify them.

## Coverage map

| Requirement | Tests |
| --- | --- |
| 004 t037/t039/t049 forced failures | `test_004_failed_ordinary_messages_finalize_without_lasting_hold` (both the no-instruction and the technical-exhaustion scenarios) |
| 004-01 / 004-02 evidence | `test_004_required_authorized_evidence_and_answer[t051/t054]`, `004-01-card-replay.json` |
| 002 t073 restriction with 002-06 | `test_002_t073_supported_restriction_is_recipient_specific`, `test_002_06_marcus_appointments_coexist_with_the_restriction` |
| 002 t157 deletion with 002-09 / 002-20 | `test_002_t157_supported_deletion_completes_through_existing_erasure`, `test_002_09_current_contacts_remain_available`, `test_002_20_deleted_details_remain_unavailable` |
| Uncertainty, ambiguity, malformed responses, timeout | `test_unusable_or_uncertain_assessment_releases_after_bounded_budget`, `test_ambiguous_recipient_or_timing_is_released_not_guessed`, `test_unresolved_scope_applicability_does_not_speculatively_block`, `test_invalid_clause_is_corrected_without_salvaging_or_blocking_valid_clause` |
| Interruption, restart, budget exhaustion | `test_interrupted_call_never_holds_and_restart_cannot_reset_budget`, `test_budget_and_finalization_survive_restart_queries_and_reassessment`, `test_temporary_protection_only_while_the_assessment_is_active` |
| No grants or approvals | `test_fallback_cannot_create_grants_bindings_or_approvals` |
| Preservation of other controls | `test_preserves_independently_validated_controls_from_other_events`, `test_exhausted_complete_revocation_publishes_existing_batch1_revocation` |
| Multiple clauses, partial instructions | `test_multiple_independent_clauses_publish_atomically`, `test_partial_instruction_applies_only_the_independent_adverse_clause`, `test_supported_restriction_from_mixed_message_is_recipient_specific` |
| Compound facts, exact complete-fact receipts | `test_compound_fact_applies_when_any_part_discloses_withheld_content`, `test_fact_quote_must_be_complete_and_exact_for_applicability`, `test_closed_complete_fact_grammar_applies_without_a_model_decision` |
| Later facts and grants | `test_restriction_applies_prospectively_to_later_facts_and_grants`, `test_new_grants_and_facts_do_not_escape_validated_restriction` (scoped holds) |
| Changed evidence, stale snapshots, rollback | `test_changed_original_source_evidence_publishes_nothing`, `test_stale_policy_snapshot_publishes_nothing_then_rebuilds`, `test_receipt_freshness_follows_the_complete_fact_and_source`, `test_publication_rollback_installs_nothing_and_consumes_one_call` |
| Deletion | `test_supported_deletion_uses_existing_erasure_and_prevents_resurrection`, `test_unresolved_deletion_is_never_labeled_completed` |
| Final release | `test_final_buffered_release_rechecks_a_restriction_published_meanwhile` |
| Reads never reassess; audit and metrics | `test_queries_never_reassess_or_make_assessment_calls`, `test_metrics_and_audit_distinguish_all_outcomes`, `test_skipped_permission_records_the_actual_access_hold_outcome` |
| Versioning, fresh stores, payload hygiene | `test_historical_store_policy_is_refused_not_migrated`, `test_payload_hides_identifiers_and_uses_only_the_original_prefix` |

Tests that encoded the superseded subject-wide hold were updated in
`test_scoped_holds_v19.py`, `test_recipient_revocation_v19.py` and
`test_host_tasks_v19.py`. Their non-hold protections (validated restrictions,
revocations, erasure, attribution, task authority) are still asserted.

## Benchmark data use

The episode replays ingest only the actual turn texts and speakers, in order.
Checkpoint specifications (`include`, `leak_targets`) are read only by
assertions after ingestion. Annotations, entity roles, future turns and gold
answers never reach runtime permission decisions. The scripted grants and
assessors are fixture interpretations of the actual turns and are labelled as such.

## Limitations

- **Real-model quality is unmeasured.** Clause recall, scope phrases and
  applicability verdicts come from the real model only in a fresh run.
- **Unestablished applicability is not enforced.** If neither call establishes
  that a validated scoped restriction applies to an existing fact (both calls
  fail, or the fact is beyond the 48 offered), that fact stays readable. This is
  the agreed policy, not a fail-closed fallback.
- **Pre-existing attribution guard (outside this change).** Batch 5 attribution
  withholds facts that name unknown proper names from everyone, e.g. "Suite 4C",
  "Darius" and "Cedar Bridge Recovery House" in episode 002. The 002 replays use
  a scripted complete-attribution wrapper for asserted statements, but these
  names still trip the guard. That limits 002-06/002-09 utility and means the
  002 deleted details were unreadable even before deletion. The tests record
  this explicitly (`attribution_withheld`).
- **Temporary protection is per process.** It is scoped by an in-process active
  marker. Another reader process treats an in-flight assessment as interrupted,
  which errs toward the agreed release policy.
- **The broad suite has one known failure:** a pre-existing wording assertion
  (`test_existing_legacy_grants_cannot_silently_lose_their_scope_limits`). It
  reproduces on the unchanged source snapshot; see
  `../v19-context-fixes/baseline-wording-failure.xml`.
- **No historical data was touched.** Historical predictions, scores, stores and
  exhausted budgets were not rewritten or replayed. A fresh run is required.
