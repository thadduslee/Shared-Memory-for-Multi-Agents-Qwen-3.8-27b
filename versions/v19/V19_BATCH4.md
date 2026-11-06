# V19 Batch 4: scoped control holds and preservation of approved access

Workspace: `/home/thaddus/a-mem-v19-fixes`, branch `v19-recipient-revocation`.
Batch 4 starts at **2021244**, the completed Batch 2 revision. Batches 1–2 and
the verified V19 baseline `3d0bc0c` remain ancestors. The complete batch diff is
`git diff 2021244..HEAD`; no Batch 3 implementation is assumed or imported.
Changes to interpretation and enforcement are enabled for version family **19**.
The original checkout and historical runs are not modified. No V20/V21 workflow,
task-access expansion, or domain dependency policy is introduced.

## Authorization contract

A binding is readable only when all existing checks succeed: the fact and its
attribution evidence are live/current; its recipient-specific grant is current,
active and within validity; the current candidate/grant decision permits this
fact; the accepted task, if any, remains valid; and no applicable restriction,
revocation, deletion/erasure, expiry, dependency denial, or control hold blocks
the requested action. Passing the hold predicate does not grant access. An old
approval neither exempts a binding from a new control nor transfers to another
recipient/grant. Existing ownership rules remain subject to these checks.

Each event has independent durable records for:

| Concern | Durable record | Consequence |
| --- | --- | --- |
| Processing success/failure/budget | `permission_jobs`, `permission_attempts` | Failed proposals publish no access; retries retain their spent budget. |
| Factual extraction/attribution/binding uncertainty | `fact_extractions`, `fact_candidates`, `fact_processing` | Unapproved or unresolved new facts remain unavailable; this is not automatically an authorization change to another fact. |
| Possible access-changing meaning | `privacy.control_assessments_v19`, legacy hold audit | Only intersecting bindings are held once a boundary is validated; unknown boundaries stay conservative. |

The new ledger key is `(compartment, job_id)` with JSON `contract_version: 1`.
The immutable `original` records the source digest and original authority ceiling.
Mutable assessment state and boundary are separate, with append-only transition
history, validator/reason, original-source receipts and identity/relationship
evidence. The ledger confers no authority or broad task access.

## Hold intersection

A possible adverse boundary records `subjects`, `recipients`, `actions`, `fields`,
`resources`, `timing`, and `unresolved`. `null` is a conservative wildcard; an empty
selector set means no possible intersection. `unresolved` distinguishes an unknown
dimension from a validated all-selector. The original ceiling derives from
authenticated restriction authority, accepted-task relationships, and trusted
workflow identity, never from a role, model-selected grant or question punctuation.

For a binding `(subject, recipient, action, resource, complete fact text)`, the
hold applies unless at least one dimension is **proven disjoint**. In particular:

1. Known subject, recipient, action, or resource selectors can prove disjointness
   when the corresponding binding value is known. Missing binding values cannot.
2. A field selector can prove disjointness only when the **entire** fact matches a
   closed field grammar. A recognized topic word is insufficient. Compound or
   unrecognized facts stay protected. The current fact prover covers invoice
   amounts, explicitly timed weekday appointments and pickup-time statements.
3. All current supported controls are immediate. Unknown/relative timing stays
   protected immediately. No timer, queue advancement, checkpoint, answer request
   or maintenance operation changes a hold's semantic state.
4. Original-source and identity/relationship evidence must remain live/current.
   Loss or correction of evidence widens enforcement back to the original ceiling;
   it cannot preserve a narrower exception without its proof.
5. Multiple holds combine by union: any intersecting active uncertainty denies.
   A failed event with no usable assessment uses legacy conservative protection.

These are predicates over current bindings, not lists of existing resource IDs or
grant IDs. They therefore constrain newly created grants and newly ingested facts.
The current language compiler does not narrow to individual resource IDs; its
resource selector is a wildcard, preserving this prospective protection.

The shared `permission_gate` enforces the predicate for search, read and derive,
including descendants/dependencies, processor input and result checks, cached
evolution jobs, evolution publication and buffered final release. Binding
maintenance filters pairs with the same subject/recipient/action/field predicate
and can continue proven unaffected work even while another source job is pending.
Main ingestion may still approve a matching fact; such an approval does not release
it through an active hold. The existing host-only control-processing route remains
able to assess protected sources.

Answer-service availability and absence reporting also use the scoped predicate.
Availability checks actual currently readable bindings before flagging a whole
assignment as held, so readable billing evidence is not mislabeled because another
binding is held. These diagnostic statuses cannot authorize or release data.

## State transitions and recovery

| State | Meaning and allowed transitions |
| --- | --- |
| `UNASSESSED` | Registered at authenticated ingestion, before inference. Uses the authority ceiling. Explicit original-event reassessment may validate non-control or an adverse boundary. Successful complete canonical control review/publication may commit controls. |
| `VALIDATED_NON_CONTROL` | A complete original-source positive grammar proves ordinary medication-question meaning. Creates no new authorization hold. Explicitly resolves existing broad holds for this same event, retaining their original body/reason and resolution audit. Processing can remain failed/quarantined. |
| `CONTROL_VALIDATED_NO_CHANGE` | The complete segment control review and canonical empty delta validated independently of factual publication. Existing authorized bindings remain usable if a later factual or publication step fails. Durable recovery can resume without buying another model response. |
| `CONTROL_COMMITTED` | Complete control review and canonical operations published in the existing atomic SQLite transaction. This records the outcome; current grants/restrictions still control access. |
| `POSSIBLE_ADVERSE` | A supported adverse boundary, unsupported potentially adverse meaning, or unavailable original evidence remains protected. Explicit identity refinement can narrow its boundary without removing the adverse interpretation. It does not become resolved because another event completes or a model says no-change. |

For ordinary failed events, `reassess_control_event(job_id)` explicitly re-examines
the original authenticated source. The same step runs during that event's source
processing/quarantine; queries never invoke it. It does not change failed proposal
history, manufacture grants, reset semantic attempts, or replay a historical run.

The positive medication-question grammar consumes the whole original text. It
supports explicit medication continuation/taking/stopping questions with a closed
medicine vocabulary, including the supplied prenatal-vitamin/iron event. A suffix
containing an access instruction fails that proof. Neither `?` nor question words
alone establish non-control. Complete successful model control review is another
independent assessment pathway; ordinary failed text outside the closed local
grammar retains uncertainty until a supported assessment is available.

Adverse scope compilation reuses Batch 1's grounded immediate revoke compiler,
and supports complete `stop sharing [my FIELD information] with RECIPIENT` requests,
including polite interrogatives and explicit lists. Names/relationships use the
same authenticated identity receipts as Batch 1, without consulting grants to
choose a person. Subject and field may be resolved even when recipient identity is
unknown; the unknown recipient selector then matches all possible recipients.
Unsupported trailing clauses or additional instructions invalidate whole-event
narrowing. Relative `tomorrow` retains timing uncertainty and protection.
Implicit "sharing" leaves primitive actions unresolved/conservative; explicit
Batch 1 revoke requests retain their independently validated action selectors.

Host callers may explicitly provide `identity_source_ids=[...]` when reassessing
an old event. Each must be a live authenticated observation in the same compartment.
These receipts can resolve identity/relationship references in the **original**
instruction; they cannot replace it with unrelated later prose. The original
ceiling, adverse meaning and history remain. An ordinary later utterance does not
implicitly reassess old events.

Supported adverse predicates remain active even after publication revokes today's
matching grants. A later grant cannot silently override them. This batch provides
no automatic adverse-hold retirement or reauthorization workflow: a broader future
recovery must explicitly reassess the original event and prove equivalent durable
prospective protection or a validated superseding authorization. Existing
`resolve_control_hold` operations cannot clear unknown/adverse V19 assessments.

## Persistence, policy versions and caches

The prompt ID is `permission-inference-v19-scoped-control-holds-v1`; the durable
policy marker is `v19-scoped-control-holds:1`. The additive ledger and marker are
installed idempotently for V19. Copy/restore both SQLite databases including the
ledger and metadata. Restart preserves pending/adverse predicates and resolved
history; no state is accepted or resolved merely on startup.

Old-prompt pending inference jobs still fail the existing changed-prompt check.
Do not rewrite their prompts, inputs, proposals or attempts to restart budgets.
Explicit local original-event reassessment can inspect those jobs without model
replay. Older unresolved jobs/holds lacking an assessment retain protection.
Older readers cannot safely enforce the new predicates: do not downgrade populated
stores without an explicit conversion of effective restrictions.

Assessment states and history participate in the authorization snapshot. All
changes use the existing transaction lock, invalidate processing tickets, and
invalidate transaction-local cached decisions through SQLite `total_changes`.
Commit/rollback drops caches; release rechecks current authorization after a slow
model call. Canonical no-change recognition updates its own snapshot only after
checking that no concurrent policy change occurred. Control publication and its
committed transition share one atomic transaction.

## Regression evidence

`security_tests/test_scoped_holds_v19.py` covers ordinary identifier failures,
independent factual/publication failures, explicit old-hold reassessment, questions
containing controls, Linda/Jules and field separation, unknown recipients/targets,
relative timing, compound instructions, new grants/facts, prospective revocation,
read/search/derive, processor/evolution result checks, stale final release after
revocation/deletion/hold, cache/snapshot invalidation, scoped maintenance, identity
refinement/evidence loss, and restart/audit/budget preservation.

One Batch 1 assertion intentionally changes: a failed mixed grant proposal for an
independently validated Linda-only withdrawal must retain Jules's approved access.
The hostile proposal is still rejected, no grants are changed, Linda's scheduling
access remains blocked, and her proven billing access remains available.

The final offline results and source digest are recorded in
`analysis/v19-scoped-control-holds/validation.json`: **449 passed, 1 known baseline
failure, 33 subtests passed** in 202.77 seconds. All **40 Batch 4 regressions**, the
40 Batch 1 regressions, 47 Batch 2 regressions and 10 retained ownership cases pass.
The full command and log are preserved beside that report. Compilation and
`git diff --check` also pass. The inherited ownership-prompt
failure remains outside this batch:
`test_generic_integration_can_explicitly_leave_ownership_disabled`.

`analysis/v19-scoped-control-holds/replay_004_01.py` reads the supplied evidence JSON
and constructs a disposable local V19 store using scripted extraction/binding. It
retains the two saved identifier-validation errors, explicitly validates the
original medication event as non-control, and releases both subsequently approved
BP recap sources with all three required plan details. The report is
`004-01-offline.json`. This controlled regression is not a replay of the full
historical prefix or a changed benchmark score. No external network/model calls, paid
evaluation, or historical writes are performed.
