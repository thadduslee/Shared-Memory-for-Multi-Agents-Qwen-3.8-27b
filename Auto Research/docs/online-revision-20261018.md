# Online workflow revision, 18 October 2026

This revision extends the existing working-tree changes. It does not resume or
modify historical runs, discard the previous evidence mode, change credentials,
or run a paid model. The original `serve_qwen38.sh` edit is retained.

## Verified incident

Read-only inspection of `medical-online-evidence-20261018-173025` found one
official row, one episode image, five planning attempt rows, zero repair rows,
and no Developer candidate workspace. Four proposals failed diagnosis review;
attempt five hit the Architect context limit. The first prediction retrieved
13 claims and required zero. Its recorded query facets were nonempty:
`medic`, `nausea`, `pain`, `right`, `stop`, `use`.

The rejected designs lacked `addressed_decision_rule`. The prompt requested it,
the diagnosis review required it, and the shared schema/projection omitted it.
One later design asserted empty query facets despite the recorded observation.
Pre-replay feedback incorrectly treated absent candidate observations as zero.
The final prompt estimate was 59,308 tokens against a 57,344 input allowance.
This historical run used evidence-based adaptation with benchmark measurement
separate from repair selection; it did not ensure every benchmark error repaired.

## Contracts and bounded planning

`online/proposal.py` declares `online-proposal-v2`. The prompt, normalization,
wire correction, diagnosis review, Developer handoff, attempts and experiment
records use this contract. It includes trace citations, the addressed rule,
predicted observable/change, observations with references, alternatives,
uncertainty, schema changes, migration/reingestion declarations, acceptance
checks, and the action to take when the cause cannot be established.

`REQUEST_DIAGNOSTIC` and `INSUFFICIENT_EVIDENCE` are distinct from a repair.
Diagnostics read bounded safe source excerpts and recorded observations; they
do not execute arbitrary model-requested commands. Formal observed values can
be checked against the cited incident. Missing measurements remain JSON null
(unknown), with the observation scope labelled. Actual trace IDs and known
evidence counts survive feedback. Build correction feedback includes safe
exception categories and file/line locations without source data or assertions.

Schema rejection returns field/type/guidance and the schema-declared projection
of the same proposal for correction. Unbuilt proposals are not experimental
failures. Built but unmeasured failures are also distinguished from measured
replay results. Duplicate screening applies to measured repairs, including the
deterministic probe, rather than forcing a new hypothesis after a formatting
rejection. Source/diff/replay checks remain additional safeguards.

Full attempts remain in SQLite/private artifacts. Architect views retain recent
relevant summaries, representative real trace IDs and explicit omission markers.
Local estimates and provider context errors trigger bounded compaction before
escalation. A too-small context no longer unexpectedly stops the stream.
Pre-ingestion incidents include at most three current-batch source examples
with digests and truncation markers in both the Architect context and Developer
handoff. These are untrusted source data. Batches containing deletion or negative
controls withhold these examples to avoid reintroducing removed information.

Options separately bound candidates, invalid proposals, schema corrections,
diagnostics, planner attempts, Developer corrections, context compaction,
evaluation retries, SQL corrections, infrastructure retries, and elapsed
candidate/incident time. Operation invocation counts also survive crashes.
Interrupted candidates spend attempt budget without being labelled validated
experiments. Restart reconciliation recovers counters and missing feedback from
authoritative attempt records. Operator intervention never silently resets them.

## Ingestion gaps and recovery

Scheduling cursors and exact ingested-turn membership are separate. The legacy
episode cursor is a **high-water position**, not proof of a contiguous prefix.
`unresolved_data` retains the failed range, payload, reason, version and retry
count in the same SQLite transaction that advances the scheduler. New run
directories are private (`0700`); journal, WAL/SHM and lock files are owner-only
even with a custom checkpointer path outside that directory. Exhaustion names say `after_attempt_limit`,
not a hard-coded number. Later immutable predictions record missing ranges and
`context_completeness.complete=false`.

The new default ordering policy is
`episode-id-then-turn-position-v2-with-data-tails`. It adds a data-only boundary
when an episode has records after its last question, or has no questions. In the
medical fixture this schedules 263 previously trailing records across 18
episodes: 567 boundaries, including 549 question boundaries and 579 questions.
Legacy v1 ordering remains available for resuming historical causal schedules.
`data_complete` checks exact source membership, separately from schedule completion.

`runner.append_stream(episodes, checkpoints=(), append_id=...)` durably appends
new batches, with or without questions. Stable append IDs are idempotent only
for identical content. Earlier inputs and boundary numbers cannot be revised.
The journal transaction stores the extension, digest and schedule together;
restart can reconstruct extensions from the original base dataset. Appends use
arrival order after the existing schedule and require the v2 policy.

`OnlineRunner.backfill(boundary)` retries the retained range under the current
safe version. It rebuilds the actual committed membership plus the gap in source
order, checks preserved sources/entities/derivations/isolation, reapplies current
deletions/revocations, and atomically replaces the image and resolves the gap.
Backfill questions are recorded separately as `backfill`, never as historical
official first attempts. Repeated committed backfill is idempotent. The separate
`--online-max-backfill-attempts` budget defaults to three and survives restart.

Negative security controls use the trusted baseline independently of a failed
ordinary parser. Pending controls are retained and reapplied to newly arriving
or backfilled data, including data absent when the deletion arrived. Ambiguous
revocation conservatively closes preceding episode permissions; it cannot add
an allow. New controls retain source ordering, so replay does not revoke a later
authenticated regrant. Legacy controls without ordering remain conservative.
An unauthorized typed deletion is rejected before it can become a retained
control or take effect during later replay.
Controls require the source owner or a policy-declared permission authority;
typed deletion always requires the owner. A sender's purported grant cannot
authorize itself. Source preservation checks verify sender, role, kind, time
and payload, reject invented observations, and forbid candidate-created trusted
control records. ISO timestamps are compared as instants, including offsets;
time-bounded grants fail closed when the boundary has no trusted timestamp.
Applications requiring narrower control semantics should supply an authenticated
structured control adapter. Data-source authentication is an application duty.

Frozen answer snapshots remain immutable. Replay uses each snapshot's original
turn membership, even after later backfill. Current tombstones and denials are
overlaid before replay, audit or prompt construction. Historical content that
no longer passes current controls is withheld from causal model context.

## Correctness and independent measurement

`--adaptation-mode benchmark_feedback` (alias `benchmark`) uses GateMem's scoring
information to trigger repair. Manifests/results call this `benchmark_feedback`
and mark it **benchmark-adaptive**. First attempts and repairs stay separate.
Evaluation transport failure becomes an explicitly unknown outcome after its
budget; it is neither a wrong answer nor evidence of correctness.
Benchmark correctness requires both the expected action and the category's
content checks. Wrong action and actual privacy/deletion leakage have separate
counters. A safe refusal can still be benchmark-wrong and trigger repair.

`evidence_based` retains independent invariant checks. Unannotated correctness
is `unknown`, even when all those checks pass. Trusted application/user outcome
feedback can open a repair incident; it does not manufacture an outcome for a
new candidate. `record_feedback` is a trusted application API: its caller must
authenticate the principal and outcome provenance. This repository is not an
authentication service. Repeated feedback IDs are idempotent and cannot be
reused for different observations.
Feedback binds to a recorded prediction signature. It cannot certify a changed
candidate answer. Late feedback can trigger repair after stream completion;
candidates awaiting an independent verdict stay isolated and inconclusive.

`online.measurement.measure_holdout` accepts a **separate** evaluation dataset,
a pinned workspace and backend, and writes results without access to a repair
journal, Architect or Developer. Those results are not repair-selection input.
No new held-out accuracy gain is claimed here. Do not use the first-question
regression or controlled candidate fixtures as an independent generalization set.
The full benchmark baseline checks were also used during engineering diagnosis;
they are regression feedback, not an untouched holdout.

## General planning and domain separation

`MemoryAgent` and `StreamDataset` are generic interfaces. Policy is explicit;
an inferred domain label does not grant access. `GateMemAgent` remains a
compatibility adapter, with benchmark policy isolated in `gatemem_policy.py`.
The worker receives trusted adapter policy from the host. The independent audit
reconstructs grants/restrictions and policy relationships from the trusted
header and raw sources, rather than trusting candidate-written grants. A new
permission language may require a separately trusted adapter/interpreter update;
a learned schema cannot weaken this authority.

The first answer failure was not fixed by reducing a match threshold. Planning
now treats coordinated topics as separate coverage groups, separates actions
from topic anchors, and uses source-backed instruction headings across fragments.
It retains quantities, units, timing and negation as source claims. Negative
imperatives such as “do not restart” are facts/instructions rather than questions.
Optional unrelated evidence stays outside the deterministic released answer.
Per-claim selection observations help distinguish missing facets, topic mismatch
and instruction intent. Historical lifecycle checks account for when a later
replacement superseded an older fact.

Planning distinguishes requested counts, lifecycle qualifiers and introductory
clauses from topic entities. An incidental “today” does not require every answer
sentence to contain a date. Simple source declarations and adjacent replies can
supply bounded topic hints for repeated named instruction objects (two hops,
256 source observations/hints). Pronouns can retain the preceding sentence's
source context. These are tentative relevance hints, not new facts or access
grants. They expose only labels already present in the question; answer claims
keep their own source provenance. Independent correctness feedback is still
required to establish semantic correctness. Nonmedical regressions cover these
paths as well as the original medical incident.

Generic input can carry `text`, structured JSON `data`, or an explicit operation.
Benchmark annotation removal is an adapter hook: the generic stream preserves
ordinary data keys such as `expected_action` or `judge_spec`. Episode metadata
provides identities and policy context; candidate-derived facts from that
reserved metadata observation are not accepted as answer evidence.
Structured leaf facts retain their exact source path/value; value/unit objects
stay together, and false/zero values are preserved. `replace` retires the prior
field version without erasing historical boundaries. Owner-authenticated
`delete` addresses a source turn and also works when the target arrives during
backfill. Unsupported operations, excessive depth or projection size fail
representation rather than silently dropping fields. The host independently
recomputes each structured display from the preserved source before release.

## Evolving tables and relationships

The Architect may propose tables/indexes/relationships and explain expected
benefit, migration, replay, acceptance and rollback. `requires_reingestion`
activates replay and live-image rebuilding for that candidate even without the
global replay flag. Evolvable renderer mode supports a coherent `system`
candidate across memory and renderer; both versions change and all gates pass
together. No unsafe intermediate version must be promoted.

`register_specialization(name, columns, reason=..., version=...)` creates a
versioned `special_*` typed projection. `project_fact` attaches scalar source
excerpts to a live canonical memory. Existing generic facts remain available;
the typed projection cannot become their only representation. `link_facts`
adds a source-backed relationship between canonical facts. All endpoints must
belong to one episode and remain live. Candidate migration SQL can populate
these tables and catalog entries; otherwise Developer ingestion code can
project/reingest records. Changes to a projection use a new versioned table.

Independent migration/runtime checks verify canonical provenance and supported
values. Generic retrieval remains available for older/unstructured records.
Permissions, source ownership, time, lifecycle and deletion apply through the
canonical fact. Shredding removes typed rows and edges. Deleting a derivative
also shreds its source to prevent reconstruction under another parser; this may
remove other fragments of that source observation. Other observations remain.

Existing additive, idempotent migration checks and private build gates remain.
Promotion atomically commits the version pointer, affected images and attempt
state. `safe_images`/`safe_state` retain the previous complete image set.
`rollback_last_promotion()` restores it atomically while retaining current
security controls, but refuses if later ingestion would be lost. Such rollback
requires explicit replay of those later inputs under the old implementation.

## Dynamic SQL

Enable `--dynamic-sql`. The generator receives the question and the **current
authorized projection schema**, including visible typed tables/relationships.
It can explicitly choose fixed retrieval. Candidate-editable `query_policy.py`
supplies declarative guidance; Python expressions are never executed by the
host when reading that guidance.

The host gateway is outside the candidate workspace. It materializes a bounded
private in-memory database from independently authorized source excerpts. SQL
never receives a connection to the authoritative image. Deleted, inactive,
future, other-requester and other-episode records are absent. Typed values must
also be supported by the permitted projection; all edge endpoints must be
visible. Even SELECT without WHERE cannot bypass those checks.
Partially authorized sources expose only scoped excerpts. Specialized table and
column names require fully visible source records, so the schema itself cannot
reveal an excluded classification. Phone-only grants project only the phone
field; date/time scopes withhold unrelated neighboring clauses. Candidate debug
strings are filtered independently before they enter feedback or prompts.

The gateway accepts one SELECT and bound scalar values, denies literal values,
comments, extra statements, writes/DDL/PRAGMA/ATTACH, recursive CTEs and functions
outside a small allowlist. SQLite authorizers, source-schema checks, VM/time,
SQL/parameter/column limits and row caps independently constrain execution.
Generated/virtual/view projections are not query authority. Returned provenance
IDs select canonical evidence; arbitrary generated result text is not released
as an answer. Invalid/expensive queries get bounded correction or fixed retrieval
fallback with safe reason codes. Successful query shapes/parameters, signatures,
schema signatures and returned IDs are retained in the private prediction trace;
model feedback receives a sanitized diagnostic view.

Candidate processes run with a clean environment, isolated process/filesystem
namespaces and an inherited seccomp filter. Credentials are not inherited;
network creation/connection and cross-process escape calls are denied. Local
Unix socket pairs remain available for asyncio wakeups. This requires Linux,
`bwrap` and `libseccomp.so.2`; missing isolation fails closed. Developer tool
subprocesses use bounded output, polling and process-group cleanup, including
cancellation, instead of an unbounded child-watcher wait.

Default audit budgets are 256 candidate facts, 64 released facts/24,000 text
characters and 2,048 authority source observations. Truncation is explicit;
absence outside those budgets is not proof of missing evidence. Large/new
source formats can require a trusted adapter or budget change. The system does
not prove semantic relevance or source truth from substring checks.

## Local validation

Validation uses actual SQLite operations, sandboxed workers/builds and protected
tests, plus explicitly controlled Architect/Developer/Judge fixtures. Fixtures
test orchestration, not a real model's ability to discover a repair.

The optional medical regression replays the original first 54 source turns,
uses the actual planner and trusted deterministic renderer, and passes GateMem's
local rule scorer for all seven required medication instruction groups. It also
checks that callback/triage context is absent. A nonmedical deployment plan
checks analogous action grouping, magnitudes, timing and negative instructions.
This targeted result is benchmark-adaptive regression evidence, not broad
accuracy evidence or a model-driven repair experiment.

Focused commands (run from this directory, no paid model; external tracing is
disabled for reproducibility):

```bash
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
PYTHONPATH=.:templates ARCHITECT_CODE_VIEW_MAX_CHARS=180000 .venv/bin/python -m pytest \
  tests/test_online.py tests/test_online_promotion.py tests/test_online_evidence.py \
  tests/test_online_revision.py templates/tests -q
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
PYTHONPATH=.:templates ARCHITECT_CODE_VIEW_MAX_CHARS=180000 .venv/bin/python -m pytest \
  tests/test_answer_composition.py tests/test_answer_evidence.py \
  tests/test_repair_security_contracts.py tests/test_architect_code_view.py \
  tests/test_write_file_guard.py tests/test_tests_gate_integrity.py \
  tests/test_judge_gatemem.py tests/test_gatemem_data.py tests/test_field_stripping.py -q
```

Recovery entry point (uses the run's original causal options; any non-mock
backend may incur model cost and requires authorization before launching):

```bash
MOCK_MODE=true .venv/bin/python main.py --online --thread-id NEW_LOCAL_RUN \
  --adaptation-mode benchmark_feedback --dynamic-sql --max-boundaries 1
# Later, for an unresolved range in that same run:
MOCK_MODE=true .venv/bin/python main.py --online --thread-id NEW_LOCAL_RUN \
  --adaptation-mode benchmark_feedback --dynamic-sql --resume --backfill-boundary 2
```

Do not use the historical evidence run ID for a benchmark-mode run: a causal
mode change requires a new run. No paid Architect/Developer run or empirical
model-discovered schema repair is asserted by this implementation report.

## Validation results and remaining limits

Completed local runs:

| Check | Result |
| --- | --- |
| Connected online workflow, promotion, evidence, revision and protected template tests | 197 passed, 292.63 seconds |
| Follow-up revision suite after final recovery and file-permission checks | 65 passed, 22.32 seconds (included in the connected suite's test identities) |
| Full benchmark baseline floor tests | 6 passed, 92.52 seconds |
| Other unit/tool/transport tests, excluding the four legacy batch-graph files | 664 passed, 8.85 seconds |
| Compilation, scoped Ruff checks and `git diff --check` | Passed |

The workflow tests use controlled Architect/Developer/Judge fixtures with real
storage, sandbox, migration and promotion gates. Medical and nonmedical worker
tests use real source ingestion, query planning, release checks and local rule
feedback. They do not demonstrate a real model discovering or implementing a
successful repair. The benchmark baseline floor permits a nonzero privacy error
rate; its passing status alone is not evidence of zero privacy leakage.

The final separate deterministic template measurement on all 579 checkpoints
reported 47/210 utility correct, 0/192 privacy leaks, 0/177 deletion leaks, and
343/579 correct action labels (`U=.2238`, `A=0`, `F=0`, `MGS=.2238`). These are
the benchmark's local rule signals for this implementation, not a live-model or
held-out accuracy result. Utility remains low and two `answer_redacted` cases
still produce `answer`; strict benchmark action feedback now identifies that
as incorrect even when the rule scorer finds no leaked target. This measurement
was used for engineering feedback and cannot establish broad generalization.

Across the connected, baseline and auxiliary suites, 867 distinct tests passed;
the follow-up revision suite repeats 65 of those identities.

The four excluded files are `test_graph_paths.py`, `test_infra_retry_graph.py`,
`test_linear_lineage_graph.py` and `test_regression_recovery_graph.py`. The broader
legacy batch-graph run did not finish its first happy-path test within the local
validation window and was stopped. Diagnostics showed changing callback tasks
and some graph progress; they did not establish the cause. Disabling tracing did
not resolve it. A final 50-second recheck again reached Developer completion but
did not complete the happy-path test. The same test in an isolated `git archive
HEAD` baseline also exceeded 50 seconds, while still in the Developer. That
comparison does not establish a common cause or rule out every regression.
These legacy batch paths remain unverified here. The separate online runner
workflow tests above completed.

The original historical journal still has one official answer, five planning
attempts, zero repairs and one episode image. It was inspected read-only and
was not resumed. Existing working-tree changes and historical artifacts remain.
No paid/model-driven run, independent held-out measurement, or universal
correctness/safety claim is made. Live authentication, specialized permission
languages, and semantic correctness beyond the supplied independent signals
remain application responsibilities or subjects for further bounded repair.
