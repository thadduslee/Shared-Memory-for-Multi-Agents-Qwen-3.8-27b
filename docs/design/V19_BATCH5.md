# V19 Batch 5: disclosed subjects and upstream privacy filtering

Workspace: /home/thaddus/a-mem-v19-fixes, branch v19-recipient-revocation.
Base revision: **30dc075**. Diff: git diff 30dc075..HEAD.
All fixes present in that workspace, including scoped control holds, are retained.
New behavior is restricted to permission family **19**. No historical run, score,
or original checkout is rewritten; no V20/V21 workflow or role-based access rule
is imported.

## Attribution contract

A source's authenticated participant is its author/speaker. That is provenance,
not an ownership claim. Each fact now separately records:

| Field | Meaning |
| --- | --- |
| author | Authenticated author/speaker from the original observation. |
| action_actors | Supported performers of an action; unsupported actor proposals are retained separately as unresolved. The current local actor prover supports explicit first-person receipts. |
| information_subjects | All resolved person/resource identities disclosed by the complete selected text. |
| attribution.state and unresolved | Whether subject coverage is resolved, or which names/context/coverage remain unresolved. |
| subject | Compatibility scalar: one subject when singular, null for a mixed record. Null alone is no longer the coverage verdict. |
| Permission assignment authority, participant and subject | Existing separate grant fields: who authorized, who may receive, and whose information the grant covers. None is supplied by authorship or action performance. |

The V19 model protocol requires information_subjects, attribution_complete and
action_actors in each fact slot. Existing primary-subject proposals remain in the
audit. Complete semantic review is necessary; absent or explicitly incomplete
coverage never becomes author ownership.

The host independently resolves literal identities from authenticated self
introductions, including introductions followed by a purpose clause. Exact full
names and unique first-name references are supported. Ambiguous names stay
unresolved; surnames alone, opaque-ID substrings, roles, grant holders, activity
labels and speaker recency cannot merge or identify people. Explicit references
found in the text cannot be omitted by a confident author-as-subject proposal.
Unsupported names or contextual comparison subjects remain unresolved rather
than being invented from benchmark IDs.

An authenticated source.subject is an information-subject receipt, distinct from
the author. It is a lower bound, not permission to omit other disclosed people.
An unnamed third party needs this receipt; an arbitrary earlier statement from a
grant holder is insufficient. The engine does not assign a record to a worker's
current task just because the worker has that task.

Author-self attribution requires a supported personal statement or an explicit
source-subject receipt. "I booked/filed/entered" and possession of records do not
establish a personal beneficiary. The local personal prover accepts bounded
personal-state and explicit self-beneficiary forms; unsupported statements remain
protected. Administrative records with an explicit, resolved subject can still
be read through that subject's real grant. Legitimate personal statements remain
available, including an author's genuine personal clause in a mixed-person note.
No medical vocabulary, fixture name, gold label, benchmark role, or
staff exemption participates in these decisions.

Unknown attribution is durable factual uncertainty. It does not create a broad
authorization hold on unrelated approved facts. Unresolved candidates remain
inspectable by the host but cannot acquire readable materializations or enter a
recipient index/query packet. Metrics expose unresolved_attributions separately.

## Whole records and current authorization

Each subject/grant pair receives an independent scope, attribution and restriction
review. The redundant-review shortcut is keyed by both recipient AND subject:
permission for one subject cannot cover another subject in the same record.

For a whole-record view to pass search/read/derive, every information subject must
have a current authorized binding for that exact candidate and attribution version
to the same requester/action. The kernel checks each component using the normal
grant, fact, binding-decision, task, restriction, revocation, expiry, erasure, source
liveness and scoped-hold gates. It does not treat approval as universal authority.
A bounded recursion marker skips only the repeated conjunction while checking
peer components; it skips none of those normal authorization checks.

Private source observations and bare unreviewed text cannot become recipient data
through an ownership assignment. Derived artifacts must retain dependencies, so
their entire input authorization is rechecked. Permission views require current
disclosure versions even when a cached binding or materialization still exists.

Only resolved candidates are materialized by permission publication. Recipient
search filters authorization before vector ranking or prompt assembly. The same
checks apply to reads, processing before and after model calls, evolution and
cached evolution results, and final buffered release. Old private index entries
may remain until lifecycle cleanup; they cannot enter the authorized candidate
set or answer context. Existing trusted host source-interpretation and private
embedding routes retain their existing privileges; staff sessions do not receive
those privileges.

## Exact projections and qualifiers

V19 factual segmentation retains complete asserted sources containing supported
conditions, exceptions, negation or comparison dependencies. Control segmentation
remains separate, preserving Batch 2's independent acceptance/adverse-intent
recognition and repair slots.

Existing independent sentence selections remain exact source spans. The host-only
project_information(candidate_id, [start,end], decision, reason=...) additionally
supports one complete, explicitly named positive assertion in a semicolon-separated
record. Every clause must fit the closed independent-assertion grammar. A source
with conditions, negation, anaphora or identity-comparison/routing associations
cannot be truncated by this API. It never accepts replacement prose, added labels,
a free-form summary, or arbitrary disjoint character fragments.

A projection receives its own subject review and subsequent ordinary grant/binding
review. Its text is exactly the selected source bytes; topics and relationships
start empty, so parent metadata cannot smuggle another person's association.
Recipient views expose only authorized text, not the protected full-source context,
identity receipts, source metadata or parent attribution record.

Source lifecycle lineage remains attached to the original observation. The
projection pins its parent's attribution version. A safe known-subject assertion
can be projected even when another independent clause has an unresolved identity;
that does not authorize the mixed parent. Correction, invalidation or deletion of
the parent invalidates old projections. Projection revisions are recreated from
the reviewed original, not silently upgraded. A mixed association such as "X,
not Y" or "same surname" remains an indivisible multi-subject disclosure.

## Correction and durable recovery

The additive private tables are:

- attribution_history_v19: versioned original/review/correction evidence.
- attribution_views_v19: exact subject/version/materialization references.
- attribution_pending_v19: durable protection while a prior attribution is reviewed.

Current policy and revision metadata participate in authorization snapshots.
A table entry, old approval or elapsed time never supplies missing subject coverage.

The host can call request_attribution_review(candidate_id, reason=...) to put even
a confidently non-null, previously readable attribution into REVIEW_REQUIRED
immediately. This survives restart and blocks old bindings, projections, cached
outputs and derived descendants before repair. It does not need a new model call.

review_attribution(candidate_id, decision, reason=...) explicitly reassesses the
original live source with current authenticated identity receipts. It increments
the attribution version, records the previous and replacement subject sets and
provenance, invalidates the affected positive/negative review cache and referenced
repair work, and clears processing tickets. Old materializations pin the previous
version and remain unreadable. Projection children pin that parent version and
must be recreated. Unrelated facts keep their current binding decisions.

The audit records structural before/after identities, source IDs/spans, versions
and digests; it does not introduce another archive of deleted clinical text.
Original source observations and prior permission-attempt history remain unchanged.
A repair that still lacks evidence becomes UNRESOLVED, not approved. A legacy
selector that omits newly recognized dependency context cannot be reauthorized by
relabeling; it requires fresh complete extraction. Direct review of a projection
requires reviewing its original and recreating the projection.

Corrections issue no grants and do not clear control holds, revive tombstones,
reset source-response budgets, or expand task scopes. New views require fresh
semantic binding decisions. Existing accepted-task/explicit-grant authority from
Batches 1–2 is still required for worker access.

The normal locked SQLite transaction protects correction publication. Snapshot
changes reject stale in-flight publication; total_changes invalidates cached
authorization inside a transaction, and commit/rollback discards caches. Result
checks and final release enforce a revocation, deletion or attribution correction
that happens during a slow model call.

## Fresh stores and explicit migration

Prompt: permission-inference-v19-disclosed-subjects-v1.
Policy: v19-disclosed-subjects:1.

A V19 compartment with existing resources and no attribution-policy marker is
rejected by default. Use a fresh store/compartment for new evaluations. For a
separately copied live-store repair, explicitly initialize PermissionUpdater with
attribution_migration='quarantine_existing'. This does not replay events or rewrite
their interpretation silently: it records pending review for existing candidates
and immediately protects old materializations. Inspect attribution_state() and
explicitly review original live candidates before requesting fresh binding review.

Preserve both SQLite files and all three tables/metadata when backing up.
Restart preserves unresolved and REVIEW_REQUIRED protection, migration quarantine
and resolved correction history. An old-prompt pending inference job still cannot
run under a silently changed prompt/version. Existing spent attempts stay spent.
An unknown attribution-policy version fails closed and requires a separately
reviewed conversion. Downgrading to an older reader that lacks these gates is
unsupported.

The abstract V19 regression fixture now supplies an explicit subject receipt for
its deliberately owner-specific factual inputs; it no longer relies on authorship
to establish those facts. Adversarial Batch 5 tests ingest raw, unattested events.
The Batch 4 replay adapter likewise explicitly attests its scripted BP information
subject; its original saved report is preserved, and the new compatibility replay
is written under the Batch 5 analysis directory.

## Validation and evidence limits

The completed offline suite returned **494 passed, 1 known baseline failure,
33 subtests passed** in 281.78 seconds. All **45 Batch 5 regressions**, 40 Batch 1,
47 Batch 2, 40 Batch 4 and 10 retained ownership regressions pass. The inherited
failure is test_generic_integration_can_explicitly_leave_ownership_disabled,
the unchanged ownership-prompt expectation. Exact command, log and implementation
hashes are in analysis/v19-attribution/validation.json and validation.log.
Compilation and git diff --check pass.

Focused tests are in security_tests/test_attribution_v19.py. They cover mixed
records, first-person actions and genuine personal statements, own and verified
task reads, complete multi-subject authorization, ambiguous/full/same-surname
identities, safe and rejected projections, upstream prompt exclusion, confidently
wrong attribution, staged review, slow calls, cached/derived exposure, later
updates, revocation, deletion, restart, migration and policy-version mismatches.

analysis/v19-attribution/replay_privacy.py reads the two supplied case JSON files,
uses their authenticated checkpoint-prefix source receipts in disposable SQLite,
and scripts the original author-as-subject error against ownership-only grants.
Both answer packets are empty; neither source is passed as answer-model evidence.
The final refusals therefore do not conceal a context leak. Unsupported names and
context references are reported as unresolved, not resolved using fixture roles.
This is an ownership-isolation regression, not a reconstruction of every historical
permission or a new benchmark score. No external model calls or historical writes
occur. Results are in privacy-offline.json and validation.json beside the script.
