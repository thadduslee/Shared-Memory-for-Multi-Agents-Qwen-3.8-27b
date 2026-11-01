# Grounded incremental access (v16)

The raw-stream runner and OpenRouter entry point now default to version 16.
Existing versions and historical run artifacts remain available. For a reusable
application, construct `PermissionUpdater(..., version='16')` and query through
`PermissionQueryService`. Use a fresh store to evaluate the new interpretation.

## Interpretation and enforcement

* Initial participants, resources, authorities and requester grants are empty.
  Authenticated speaker discovery supplies control authority over one's own
  information, but no requester read assignment. Model proposals supply grants.
* A short current-event review first identifies the authenticated speaker and
  assesses each affected recipient independently before operations are emitted.
  A subject grant and another worker's grant cannot substitute for the addressed
  worker's own source-supported grant.
* A combined source response independently classifies facts, reviews all current
  segments, proposes the complete control transition and reviews recipient bindings.
  The model chooses subjects, scopes, recipients, authorization meaning and scope
  applicability. The host performs identity, receipt, lifetime and lineage checks.
* Explicit permission, a particular subject request, and an authenticated response
  to a worker are distinguished. A responsive worker grant must cite that worker's
  actual prior message and a later subject authority receipt. The host never selects
  a substitute recipient. Self requests cannot authorize somebody else.
* The model can explicitly select a self-activity rule by identifying an activity
  the subject actually sought or accepted and citing that subject's statement.
  The generic rule covers their own responsive instructions/results/plan/updates;
  it does not limit them to the literal symptom or requirement first reported.
  Bare ownership/assertions activate nothing. Every other recipient still needs
  an independent field grant. No initial or query-created self permission exists.
* A new grant must cite the current authenticated authority event. An unrelated
  worker update cannot recycle an older statement into fresh consent.
* The model can select a necessary-service-information rule for an actual worker
  whose service the subject accepts. This requires the worker's own prior receipt
  and the current subject acceptance. It covers necessary responsive information
  and subject-provided operational contact instructions for that same service;
  original exclusions and independent field reviews still constrain disclosure.
  Explicit time/place-only consent uses a literal field grant and cannot expand
  through this rule. A worker's question about someone else grants no self access.
* Fact attribution distinguishes the affected subject from the speaker. A worker
  booking a customer's appointment does not make it the worker's personal fact.
  A permitted field set is a ceiling: a time-only fact need not also contain every
  other permitted field, such as a room or parking detail.
* Original authorization statements and worker interactions remain in subsequent
  binding inputs. A scope label cannot justify facts beyond that source evidence.
* Real-world instructions are distinct from information-disclosure restrictions.
  Communication-channel constraints do not suspend unrelated reader assignments.
* The host derives operation indexes from the proposed transition. Per-segment
  unresolved controls still fail closed. Invalid authority, expiry, revocation,
  deletion, erasure, source liveness and final release checks are retained.
  Scheduled-expiry validation preserves its operation context; a valid expiry
  no longer crashes the sliding-window evidence checker and creates a hold.
* Every binding first selects an exact quote prefix from the supplied facts. The
  host checks that it matches the selected fact ID. Rationale precedes the decision.
  Third-party disclosures require a separate focused review within the two-call
  source budget. If control repair consumes that budget, unverified fields remain
  protected for a later update.
* A scoped denial cannot implicitly suspend a whole grant. V16 uses field
  reviews for scoped denials; full withdrawal requires an explicit revoke or
  suspend decision with authority evidence. Current review fingerprints prevent
  stale positive bindings from bypassing a new restriction.
* A later negative field review removes an earlier positive binding. V16 reads
  require a current positive review fingerprint, including when an old view or
  derived artifact still exists. Relevant control changes use the bounded repair
  call to refresh affected fields; other subjects/recipients remain independent.
* Facts remain protected without a grant. A binding failure leaves that fact
  unavailable without imposing an unrelated subject-wide control hold. True
  unresolved controls retain protective holds; they are never silently cleared.
* Optional null metadata no longer crashes list expansion. Rephrased duplicate
  binding explanations retain identical validated decisions; contradictory
  decisions remain rejected.
* Missing-information classifications use required named slots, preventing
  duplicate request indexes. V16 queries retrieve up to 24 permitted records and
  enumerate broad plans/schedules so a single best match does not hide other items.
  V16 answers first classify every supplied evidence record by relevance using
  required numbered slots. Current, continuing, retired and conditional plan
  collections then precede prose. This preserves separate confirmed bookings
  when a later summary mentions only another event. Required arrival/check-in times and
  preparation qualify an event even when the question asks only for time/location. Short citation handles reduce request and
  output tokens; canonical IDs are restored before host validation/release.
* Missing information is classified against live semantic permissions, including
  grants with no remaining readable facts. No requester query can create access,
  mutate policy or trigger ingestion repair.
* Access expiry is distinct from an event's scheduled date: while a grant remains
  live, it may permit the time of an event after the grant's expiry, unless the
  original authorization explicitly restricts event dates themselves.

## Work bound and budgets

Ordinary source processing uses one semantic call and at most one focused repair.
Binding decisions remain cached, invalidated by relevant policy/evidence changes.
The binding batch is at most 12 pairs; overflow remains durable and unavailable.
Protected history is routed by local embedding similarity to the event and grant
scopes, with an age lane for progress. A new recipient's history is considered
even if somebody else already has a grant. Routing cannot authorize a fact.
Existing indexed vectors are reused; extra routing vectors live only for one
update and are cleared on success/failure. Original authorization receipts are
deduplicated. When extraction/controls succeed, a second call reviews only the
missing binding pairs with a read-only output grammar. Each focused review puts
the selected fact beside that recipient's grant, original authorization statements
and applicable restrictions, avoiding cross-recipient scope confusion. The bounded
router considers distinct recipients before overlapping grants for the same fact.
V16 permits up to 2,048 output tokens by default, bounded by the permission cap.
`AMEM_INCREMENTAL_MAX_TOKENS` overrides this limit; v15 retains its 1,024 default.
The limit is a ceiling: short ordinary responses do not consume the whole budget.
Large arbitrary documents or policy backlogs do not have a constant-time guarantee.

## Reproduction

The comparison uses the same Qwen memory/answer model, GPT-4.1 judge, embedding
model and chronological checkpoint IDs as the original run. GateMem's dataset card
states that all benchmark episodes are synthetic and contain no real patient data.
Evaluator annotations are supplied only to the scorer, after prediction.

Run a fresh first-25 evaluation using existing private API configuration:

```bash
AMEM_OPENROUTER_PROVIDER_SORT=latency AMEM_OPENROUTER_PROVIDER_IGNORE=deepinfra \
PYTHONPATH=. /home/thaddus/gatemem/.venv-nb/bin/python -u \
  -m evaluation.gatemem_medical.openrouter \
  --permission-version 16 --max-checkpoints 25 \
  --run-dir runs/gatemem-medical-v16-first25
```

The recorded evaluation used for development adds an auditable model trace:

```bash
AMEM_OPENROUTER_PROVIDER_SORT=latency AMEM_OPENROUTER_PROVIDER_IGNORE=deepinfra \
PYTHONPATH=. /home/thaddus/gatemem/.venv-nb/bin/python -u \
  analysis/over-refusal-v16/run.py --checkpoints 25 --version 16 \
  --run-dir analysis/over-refusal-v16/new-candidate
```

`compare.py` requires the same chronological prefix and identical models/dataset.
It recomputes baseline metrics and source latency for that prefix, rather than
comparing with the original run's later global summary. Development probes are
retained; their results are not substituted for the final candidate.

Final measurements and remaining limitations belong in `REPORT.md` after scoring.

## Reproducible provider routing

OpenRouter requests keep `require_parameters=true` and latency sorting. Optional
`AMEM_OPENROUTER_PROVIDER_SORT` accepts latency/throughput/price;
`AMEM_OPENROUTER_PROVIDER_IGNORE` accepts comma-separated provider slugs.
The complete provider preferences are recorded in the run manifest and cannot
change on resume. The final comparison uses `AMEM_OPENROUTER_PROVIDER_IGNORE=deepinfra`
after observed long-prompt calls through that provider were slower; provider
availability and speed vary, so this is a measured configuration, not a universal
provider ranking. A throughput-sort experiment was slower on the generic workflow.
Models, structured-output requirements and access checks are unchanged.
See [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).


## Current full-run launch (2026-11-01)

The final implementation uses paired model-selected activity operations, exact
literal field quotes, mandatory indexed focused-review verdicts, and separate
current-control versus own-binding tables. Other-recipient binding opinions
are omitted from the main call; independent focused review remains mandatory.
A matching-value restatement cannot retire fuller dates/cancellation details.

The requested fresh full run is in tmux session `gatemem-v16-full-1101`.
See `runs/gatemem-medical-v16-openrouter-full-20261101T171100+0800/FOLLOW.md`.
`full_session.py run RUN` runs `run.py --full --version 16 --stop-on-leak`;
`full_session.py watch RUN` shows progress and creates the first-25 comparison.
--full deliberately omits the checkpoint cap so source-only tails are processed.
Recorded routing: latency sort, ignoring deepinfra, coreweave and reka.
