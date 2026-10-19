> The current contract, recovery, SQL security and validation details are in
> [the October 18 workflow revision](online-revision-20261018.md). Historical
> results below are retained as observations of their original implementations.

# Online GateMem memory evolution

## Adaptation and evaluation modes

Choose `--adaptation-mode benchmark|evidence_based`. Both the option and the
assessment mode are persisted in the run manifest; changing mode on resume is
rejected. The default remains `benchmark` for existing commands and integrations.
The benchmark repair path below uses hidden Judge measurements and must be
reported as benchmark-driven adaptation, rather than real-world online learning.
Only Judge/evaluator measurement code receives annotations. Raw Judge reasons
never enter the online Critic, Architect, or Developer.

`evidence_based` works when every annotation, including query categories, is
absent. It calls no Judge for repair triggers or promotion. A trusted audit
checks observable source support, plan invariants, temporal/lifecycle state,
authorization and deletion. Its `passed` compatibility field means only that
those checked invariants found no verified defect. It is **not** a correctness
or usefulness score. Its scoreboard reports `MGS`, utility and accuracy as
undefined, and separately reports assessed and invariant-compliant attempts.
The legacy boundary state name `passed` means only that processing reached its
normal terminal state. `evidence_disposition.json` and `critic_report.json`
retain unresolved hypotheses, verification status and unknowns explicitly.
Optional `--benchmark-measurement` stores Judge measurements in separate durable
state; those results cannot affect evidence-based repair or enter repair prompts.
This option requires an annotated measurement dataset.

The CLI still uses the configured dataset loader. Applications with an ordinary
unannotated stream can construct `MedicalDataset(episodes, checkpoints)` and
`OnlineRunner(..., options=OnlineOptions(adaptation_mode="evidence_based"))`.
The container name is historical; scheduling needs only public episode/query IDs,
requester, question and turn positions. No expected answer is generated.

## What the online Critic establishes

The online Critic uses a structured evidence packet for each requester/query,
with a deterministic audit followed by model interpretation when models are
enabled. The Critic has no tools or write capabilities. Retrieved text is marked
as untrusted data, never instructions. Reports contain observed issues, evidence
references, stage/competing hypotheses, mechanism, verification basis, missing
evidence, a discriminating check, protected invariants and prior-attempt lessons.

Three statuses have different consequences:

- `verified_defect`: a trusted reproducible check identifies a broken invariant;
  it can trigger automatic repair.
- `suspected_defect`: relevance, interpretation or causal attribution remains
  uncertain. The Critic investigates the bounded permitted history and reports
  a discriminating check. This does not automatically become a product repair.
- `insufficient_evidence`: no defensible defect was established. The question
  remains unresolved; an absence of evidence is not declared success.

Model confidence and agreement never certify defects. An empty required plan
for an answering action is reproducibly invalid. A matching source fragment
missing from retrieval is observable, but lexical similarity alone cannot prove
that the fragment answers the user's question, so that diagnosis stays suspected.
Evidence consistency cannot establish that a source itself is true. Real-world
accuracy and usefulness remain unknown without independent observations.

The application can call `runner.record_feedback(...)` after a first attempt to
record a content-free user or outcome signal. It must authenticate the caller
and the provenance of that signal. The API enforces requester scope and boundary
availability, accepts no expected answer/Judge text, and leaves first attempts
immutable. Negative signals add an unresolved causal hypothesis; positive ones
cannot erase independently verified violations. Historical replay cannot invent
new real-world outcomes for a candidate.

## Evidence visibility and audit boundaries

`online/evidence.py` reads a private copy of the frozen boundary SQLite image,
opens SQLite in read-only/query-only mode, and imports the trusted implementation,
not candidate code. It scans up to 256 source-derived records independently of
candidate ranking/top-k, exposing at most 64 permitted records and 24,000 source
characters. Oversize records are omitted rather than truncated into new facts.
A separate content-free lookup checks up to 64 emitted record IDs against
ownership, authoritative policy, lifecycle, time and tombstones, even beyond the
scan window. Truncation and missing boundaries are explicitly inconclusive.

The packet includes the visible question/requester, permitted historical facts
and provenance, retrieved IDs, selected/optional/required claims, independent
selection decisions, content-free lifecycle/permission exclusions, trusted
action, checked renderer behavior, deterministic fallback, checked final answer,
scoped experiment lessons and available independent feedback. Candidate bodies
and arbitrary debug/Judge dictionaries are not trusted visibility envelopes.
Claims must match permitted source records before being quoted. Unvalidated
released text is represented by a signature rather than forwarded verbatim.
No denied/deleted bodies, other requesters' evidence or future observations are
exported. Permission/relationship provenance and validity are checked at the
query boundary. The audit records unknowns, including unrecorded events and
source truth. It cannot recover source events that were never ingested.

Experiments and reports are local run artifacts with the same filesystem access
boundary as the existing run database. Prompt lessons are additionally isolated
by episode and requester and revalidated against currently permitted record IDs.
A revoked/deleted source therefore cannot be quoted from an older experiment
into a later repair prompt. This is not a multi-tenant artifact-serving API;
applications must enforce access controls when serving files to users.

## Repair, promotion and experiment memory

Evidence-based incidents are scoped to one episode/requester. Repair covers the
whole causal chain from source/ingestion through retrieval, authorization,
selection, required plan, rendering and release. A rejected renderer proposal
followed by a fallback does not establish a renderer-caused released-answer
failure. Plan failures take precedence over renderer telemetry. Latest replay
evidence can revise the route. Architects may describe dependent stages, while
coherent memory and renderer changes may use a `system` candidate in evolvable mode.
Every promoted candidate must pass all gates together; unsafe intermediate changes remain isolated.

The ordered gates are shared proposal validation/novelty, build and protected
invariants, a cheap deterministic memory-decision probe, focused historical
replay, then broad preservation/regression checks. An unchanged memory decision
rejects an evidence-based memory candidate before rendering/Judge expense.
Replay cache identities include adaptation mode and the trusted assessment
version; stochastic renderer sampling retains separate vote indexes. Promotion
requires demonstrated resolution of the original checked defect, no new required
gate violations, and preservation of established answers. Switching to silence
cannot resolve an answering-plan defect. Previously compliant replay answers
that change fail the conservative evidence-mode preservation gate. This gate
may reject a legitimate wording-only improvement; it favors preservation until a
more specific independent equivalence check is supplied.

Authoritative policy JSON is protected from candidate edits. Frozen negative
invariants cover unassigned roles, cross-patient assignment, expired/revoked
consent, time-limited assignment and deletion. Improvements to metadata parsing
must retain those specifications. A role alone cannot replace assignment,
delegation or consent. Model-authored safety assurances are not validation.

`online/proposal.py` is the shared wire schema. A singleton work-order string and
a mapping of named, nonempty string safety checks normalize to lists; booleans
and malformed substantive values remain invalid. Shape errors identify field
path, expected/received type and correction guidance without echoing raw values.
At most two wire-correction calls occur within the same planner attempt before
ordinary bounded planning resumes. They allocate no candidate and do not consume
complete failed-candidate attempts.

`experiment_memory.json` and `.md` retain up to 128 readable entries, including
hypothesis, evidence, component/change, predicted effect, bounded source diff,
measured effect, safety results, conclusion and next investigation. Relevant
prompt history is capped at eight revalidated entries. Developer instructions
include the hypothesis, prediction, protected checks, Critic report and prior
experiments. Equivalent rejected proposals are checked before Developer work;
evidence-mode index renames have the same canonical mechanism. Duplicate feedback
explains the earlier hypothesis and measured result. Source-diff and replay
identity checks remain backstops; semantic equivalence detection is conservative,
not a complete equivalence prover. Existing finite planner/candidate limits apply.

## Local validation and migration

No paid models, historical resume or full benchmark is needed:

```bash
ARCHITECT_CODE_VIEW_MAX_CHARS=128000 .venv/bin/python -m pytest -q \
  tests/test_online_evidence.py tests/test_answer_composition.py \
  tests/test_repair_security_contracts.py tests/test_architect_code_view.py
PYTHONPATH=templates .venv/bin/python -m pytest -q -c /dev/null templates/tests
.venv/bin/python -m pytest -q tests/test_online.py tests/test_online_promotion.py
```

The evidence loop fixtures simulate deterministic candidate behavior and use
independently specified controlled examples. Their promotion results validate
orchestration/gates, not real-world accuracy gains. Candidate invariant tests
continue to run in the existing filesystem sandbox.

Legacy manifests missing adaptation mode mean `benchmark`; scores and immutable
first attempts are preserved. New fields are additive. New template behavior is
seeded into new runs; frozen historical workspaces are not rewritten. Do not
change an experiment's mode in place. Use fresh run directories for comparisons.
The offline Architect's default source-view budget is now 128,000 characters so
the expanded protected template tests fit. An existing `.env` override of 120,000
still takes precedence and should be increased when validating the complete
expanded baseline. This implementation does not alter that local override.

The inspected `runs/medical-online-full-20261018-122105` snapshot stops while
repairing boundary **11 of 549**. Its repeated schema errors, duplicate changes
and routing evidence are historical observations, not a completed experiment.
Its files and the user's existing shell-script change are preserved.

Before any full run, compare two fresh, small runs over the same controlled
unannotated stream: evidence mode with `disable_redesign=True` and with adaptation
enabled. Include two or three boundaries with known source replacements,
irrelevant additions, equivalent questions, requester changes and a reproducible
plan defect. Compare first attempts separately from repair results, invariant
violations, required coverage, support, resolved/unresolved defects, preserved
answers and cost. If using an annotated benchmark subset for independent
measurement, isolate its scores and use repeated renderer samples when needed.
Obtain separate authorization before paid/model or full-benchmark execution.


The remaining sections describe the existing benchmark-oriented workflow and shared mechanisms.

## Grounded answer composition and ownership

Online answering has one explicit trust boundary:

```text
visible query -> retrieval -> authorization -> responsive selection -> required answer plan
              -> untrusted renderer -> fixed contract -> released answer
```

The memory component owns the trusted action, authorization, selected claims,
required coverage, provenance, and deterministic `fallback_answer`. Each claim
has a stable opaque ID, source record/observation, visible-query facets, and
source-preserving critical values. The legacy `answer` field remains only as a
compatibility alias for older workers; it is not the final online answer.

Retrieved evidence may be broader than authorized evidence; authorized evidence
may be broader than responsive selected claims; and selected claims may include
optional context that is not required coverage. Query-conditioned selection uses
only the visible query/requester, active authorized evidence, lifecycle/temporal
state, structural facets, and permission state. It distinguishes typed roles
such as appointment time and arrival time. Only required approved claims enter
the deterministic fallback.

The renderer receives the visible query and selected approved claims only. It
returns only sentence-level wording with claim IDs. It does not choose or echo
an action and does not calculate used claim or record IDs. Trusted code inserts
the memory action and derives both used-ID lists from sentence attribution. It cannot see
denied/obsolete records, hidden scorer fields, expected answers, or Judge
reasoning, and it cannot choose the action. Refusal and no-memory decisions use
fixed policy text and are never sent to the generative renderer.

`renderer_contract.py` is trusted infrastructure outside every candidate
workspace. It validates the renderer schema, claim IDs and authorized
provenance, required-claim coverage, sentence attribution, typed complete
values (dates, times, phones, identifiers, doses, quantities and percentages),
negation/status, and deterministic lexical support. Renderer attribution alone
is not proof. Unrestricted natural-language generation cannot be mathematically
proven grounded, so malformed, unsupported, altered, incomplete, or
inconclusive output fails closed to the source-preserving concatenation of all
authorized required claims. Artifacts record `validated_renderer` or
`deterministic_claim_fallback` and a content-free conflict reason.

The renderer remains part of the Evaluator's answer-composition phase; it is
not an independent top-level LangGraph node.

## Renderer modes and component versions

Every online run pins `--renderer-mode locked|evolvable` in its durable
manifest. `locked` is the compatibility-preserving default: repair candidates
cannot edit the benchmark renderer, and a true renderer-only failure is
escalated instead of being misrouted to memory fallback wording. `evolvable`
allows only the declarative `renderer/renderer.json` artifact to change. The
fixed contract is never copied into a candidate workspace and candidate Python
is not loaded into the orchestrator.

The effective version is a tuple:

```json
{
  "memory_version": "memory_v0001",
  "renderer_version": "renderer_v0001",
  "contract_version": "fixed_contract_v0002",
  "renderer_mode": "locked"
}
```

A candidate normally changes one component; a `system` candidate in evolvable mode may change both together. Memory candidates may change only
`memory_system/` (and may carry an idempotent migration); renderer candidates
may change only `renderer/` and may not carry a memory migration. Mixed changes require an explicit `system` work order and joint validation. Memory replay freezes the renderer artifact;
renderer replay runs parent and candidate wording from one frozen trusted
memory decision and answer plan. Promotion advances each changed component version and retains the fixed contract version.

Legacy manifests default missing component fields and renderer mode to the
historical locked/v1 values. Resuming with a different renderer mode is refused.
Official historical predictions and scores remain immutable through migration
and promotion.

Failure diagnosis uses the first failed stage with this precedence:
infrastructure/inconclusive vote, isolation, ingestion/state reconstruction,
safety regression, authorization/action, retrieval, fact selection/plan,
renderer contract, grounded-render utility, then unknown/escalate. It uses
`INGESTION`, `AUTHORIZATION`, `RETRIEVAL`,
`FACT_SELECTION`, `RENDERING`, `INFRASTRUCTURE`, or `ESCALATE`.
`FACT_SELECTION` routes to memory claim selection. `RENDERING` routes to the
renderer only in evolvable mode and escalates in locked mode.
`INFRASTRUCTURE` never creates a product candidate. Diagnosis review verifies
component ownership, editable scope, causal effect on the final observable,
trace citations, novelty, and the hidden-field wall.

Run a new evolvable-renderer experiment with:

```bash
.venv/bin/python main.py --real --online --renderer-mode evolvable --thread-id <new-run-id>
```

Run the benchmark-locked mode explicitly with:

```bash
.venv/bin/python main.py --real --online --renderer-mode locked --thread-id <new-run-id>
```

Reports from locked and evolvable runs disclose their renderer mode/version and
should not be compared without noting that difference.

Online mode is a durable prequential experiment with two independent
milestones: first make the exact staged data safely representable and commit
it, then make the required answers pass. It persists the official first
attempt, judges it, and repairs at that boundary when required.
Official predictions and scores are immutable. Repair predictions are stored
separately. A scored repair affects only later official predictions; a
pre-ingestion repair is promoted before that boundary's first official attempt.
Ordinary repair failure never blocks the stream forever: after the configured number of failed
candidates (default five) the boundary records an explicit terminal failure and
scheduling continues from the last safe committed version.
The batch/non-online research loop has separate routing and budget behavior.

## Running and resuming

```bash
MOCK_MODE=true .venv/bin/python main.py --online --thread-id online-demo --max-boundaries 3
MOCK_MODE=true .venv/bin/python main.py --online --thread-id online-demo --resume
.venv/bin/python main.py --online --print-graph
```

For real model calls:

```bash
.venv/bin/python main.py --real --online --thread-id medical-online
```

Useful online controls are:

- `--max-boundaries N`: process at most N additional scheduled boundaries in
  this invocation. An exhausted pre-ingestion boundary can advance without an
  ingestion commit, and that distinction is recorded explicitly.
- `--replay-window N`: cap the recent visible outcomes included in Architect
  context. Mandatory broad replay still covers every observed scored result.
- `--utility-regression-tolerance F`: maximum decline among previously passing
  utility cases. Privacy and deletion regressions are always rejected.
- `--online-max-failed-candidates N`: complete candidate failures allowed at a
  boundary (default `ONLINE_MAX_FAILED_CANDIDATES=5`).
- `--online-max-invalid-proposals N`: retained for resume compatibility with
  older journals. Planner abstentions no longer consume this allowance.
- `--checkpointer-path PATH`: address the authoritative SQLite journal
  directly. With `--resume`, its manifest determines the artifact directory.
- `--retry-blocked`: after the operator fixes an infrastructure, prompt-sizing,
  or release-validation problem, retry the same durably blocked boundary. These
  failures do not increment the failed-candidate counter.
- `--abandon-blocked`: legacy compatibility for old journals already in a
  `repair_blocked` state. New ordinary repair exhaustion advances automatically.
- `--disable-online-redesign`: run compatible boundaries without redesign and
  durably block, without advancing, when a repair incident occurs.

Online repair is finite but deliberately generous. Separate configurable limits
bound a model request, Developer turn count/episode, candidate, incident, replay
vote, and replay concurrency. Defaults are
`ONLINE_SINGLE_REQUEST_TIMEOUT_S=900`, `ONLINE_DEVELOPER_TURN_LIMIT=40`,
`ONLINE_DEVELOPER_EPISODE_TIMEOUT_S=3600`, `ONLINE_CANDIDATE_TIMEOUT_S=7200`,
`ONLINE_INCIDENT_TIMEOUT_S=21600`, `ONLINE_REPLAY_VOTE_TIMEOUT_S=900`, and
`ONLINE_REPLAY_CONCURRENCY=4`. Limit exhaustion is durable and non-promotable,
does not become product-design feedback, and can be retried by an operator.
Token and elapsed-time use remain recorded. A candidate
whose build still fails after `ONLINE_MAX_CORRECTION_ROUNDS` (3) Developer
corrections is charged exactly once as an implementation/build failure; the
correction rounds themselves do not increment the boundary counter. Provider
request/context limits still apply, and transport retries remain finite so a
frozen request cannot hang forever. Exhausted transport retries produce
`infrastructure_blocked`; they are classified separately, consume no candidate
attempt, and do not advance the boundary. Architect prompts are conservatively
estimated before dispatch using `ARCHITECT_CONTEXT_WINDOW_TOKENS` and
`ARCHITECT_CONTEXT_OUTPUT_HEADROOM_TOKENS`. A local or provider-reported
context overflow triggers bounded compaction retries, then an explicit escalation.
It never spends a product candidate attempt or a transient transport retry.

The release gate verifies every selected evidence and claim source by record ID,
using the trusted authorization and provenance checks. The historical audit's
scan, record, and text limits only bound diagnostic reports. An official
prediction that fails validation stops at `validation_blocked`, retains its
ingested snapshot and previous official results, and writes
`events/boundary_NNNN/release_validation_failure.json`. No rejected prediction is
recorded or scored. After fixing the cause, use `--resume --retry-blocked` to
retry that prediction without ingesting the boundary again.

Resume requires the same dataset, mock/real backend, ordering policy, replay
window, and utility tolerance. Existing run IDs require `--resume`, and the
journal lock prevents simultaneous owners. Legacy online budget fields in an
old manifest are recorded under `legacy_online_limits_ignored` and do not govern
new repair work.

After verification, the v4 run can be resumed with this exact command (do not
start it while tests or implementation work are still in progress):

```bash
.venv/bin/python main.py --real --online --thread-id medical-online-full-20261010-v4 --resume
```

## Boundary state machine

The stream order is `episode-id-then-turn-position-v1`: episode IDs sort
lexically, turns use their recorded array position, and checkpoint IDs only
break ties within a boundary. No future checkpoint or hidden annotation is
used for scheduling or repair.

```text
stage exact incoming delta
  -> committed-version ingestion probe
  -> if incompatible/unrepresentable: pre-ingestion repair incident
  -> atomically commit image + cursor when safe
  -> freeze committed version
  -> persist every official prediction at the boundary
  -> persist every official score
  -> if any score fails: scored-checkpoint repair incident
  -> pass/promote, or record terminal failure after five failed candidates
  -> advance scheduling
```

Benchmark success outcomes are `passed` and `repaired_and_passed`.
`evaluation_unknown` and `checked_invariants_truth_unknown` do not establish correctness.
Scored repair exhaustion is `unrepaired_after_attempt_limit`; the immutable wrong
official answers and scores remain official, but the safely represented data
and representation-capable parent remain committed. Pre-ingestion exhaustion is
`unrepresented_after_attempt_limit`; the exact staged delta remains uncommitted,
the committed memory image and ingestion state do not change, and the journal
records `schedule_advanced=true` with `ingestion_occurred=false`. Subsequent
boundaries use the last committed image and their own scheduled delta; skipped
turns are not silently replayed or treated as present.

If at least one safe partial promotion occurred before scored-repair
exhaustion, the terminal boundary outcome is
`partially_repaired_with_remaining_failures` with terminal reason
`unrepaired_after_attempt_limit`; the latest partial version stays committed.

One durable `failed_candidate_attempts` counter spans both phases at a
boundary. A successful representation candidate and the subsequent wrong
official answer add nothing to it. Only a complete rejected candidate adds
one; Developer correction rounds add nothing; infrastructure failures add
nothing. Thus four failed representation candidates followed by successful
representation leave exactly one failed-candidate allowance for scored repair.

Each durable incident records its ID, triggering boundary and type, exact
staged delta and digest when applicable, target checkpoint IDs, unresolved
targets, safe parent, candidate and feedback histories, signatures, current
phase, and promotion history. Its explicit resumable phases are:

```text
architect -> diagnosis_review -> developing -> build_validation -> targeted_replay
          -> broad_validation -> promotion
          -> rejected_with_feedback -> architect
          -> infrastructure_blocked
          -> planner_abstention -> architect -> escalated (after four meaningful attempts)
          -> unrepaired_after_attempt_limit | unrepresented_after_attempt_limit
```

For a scored failure, the runner also creates a deterministic, durable
`architect_incident_dossier`. It contains the visible query/requester, official
action and rendered answer, only the evidence actually released, and an
allowlisted per-record decision trace. Trace entries identify the record and
source, status, structural concepts, event-time features, selection outcome,
decision stage/rule, record-validity bounds, and applicable permission IDs.
Permission validity at query time is shown separately from record event dates,
record-validity bounds, and record lifecycle status. Unauthorized records never contribute raw text. Unsupported query
facets and a compact timeline make omissions diagnosable without exposing
scorer annotations.

The Architect receives that dossier on the first proposal and every retry. It
must return one diagnosis, one focused goal, a one-item work order, exact
dossier `trace_id` citations, the cited decision rule it addresses, and a
prediction of an observable targeted-replay change. A deterministic read-only
diagnosis review runs before any workspace is created or Developer is invoked.
It verifies citations, rule alignment, the observable prediction, novelty
against rejected attempts, and the hidden-field boundary. Rejections are
persisted and returned to the Architect as structured feedback.

The Architect classifies every scored mismatch as `INGESTION`, `AUTHORIZATION`,
`RETRIEVAL`, `FACT_SELECTION`, `RENDERING`, `INFRASTRUCTURE`, or `ESCALATE`.
Actionable classes require one falsifiable
proposal naming the subsystem/file/function, hypothesis, one work order,
observable replay change, target checkpoint IDs, and safety checks.
`NO_VIABLE_REPAIR`, malformed output, duplicate output, and untestable plans are
planner abstentions: they create no workspace or version and are not candidate
rejections. Schema-only corrections and semantic duplicates do not consume a
meaningful planner attempt, though their own finite invalid-proposal allowance
prevents spinning. After four causally distinct attempts (or the invalid limit),
the runner writes one privacy-safe incident
to `escalations/`, marks the boundary `escalated`, and continues scheduling.
Neither failed-candidate nor invalid-proposal counters are consumed.

One actionable proposal creates one isolated workspace from the safe committed
parent. Target checkpoints run first. Broad privacy, deletion, and
previously-passing regression validation runs only after at least one target
improves, and promotion requires that improvement with no regression.

The same `DeveloperSession`, conversation, toolbox, and workspace own that
candidate. A compile, invariant-test, or candidate-test failure is returned
verbatim to that session; the failed gate is reopened and rerun after correction.
Ordinary implementation failures do not call the Architect. The Developer
returns to the Architect only for an explicit unworkable conclusion; completed
but ineffective or unsafe candidates are returned by the runner after replay.

## Targeted gate, then broad validation

Once build and migration checks pass, the runner executes only the target gate:

- A scored incident replays every unresolved checkpoint against paired parent
  and candidate images from the same historical boundary.
- A pre-ingestion incident applies the exact staged turns to the migrated
  candidate image and verifies faithful representation. Current-boundary answer
  checks are deliberately deferred until after atomic promotion and ingestion;
  those first post-commit predictions and scores become the immutable official
  rows.

Paired scored results retain evidence record IDs and content hashes, final
action, rendered answer, score, pass/fail, evidence/answer/score changes, and
failing-to-passing or passing-to-failing transitions. If any unresolved target
still fails, broad replay is skipped and the rejection reason is
`wrong_answer_no_targeted_improvement`.

For scored repair, any non-empty subset of unresolved targets may open broad
validation. Mandatory broad gates cover:

- compilation, original trusted tests, candidate tests, and idempotent SQL
  migration;
- preservation of old tables, rows, grants, relationships, tombstones, and
  shredded keys, plus SQLite integrity;
- episode and patient isolation and safe reopening under candidate code;
- every observed privacy, deletion-safety, and utility checkpoint, including
  all unresolved targets;
- no checkpoint/query-specific executable constants or benchmark
  specialization;
- hidden-field protections at Architect, Developer, evaluator, replay, and
  feedback boundaries.

A scored candidate is eligible when it fixes at least one unresolved target,
does not regress a current or historical pass, introduces no new failure or
privacy/deletion leak, and every mandatory gate passes. A strict subset is
committed atomically with attempt status `partially_promoted`; fixed targets
are marked resolved, the remaining target set is retained, and the next
proposal is built from that partial version. Partial promotion does not consume
a failed-candidate allowance. Later exhaustion retains the latest safe partial
parent.

Repair-model feedback is a content-free allowlist. It includes a failure
category, phase, subsystem flags, record counts, target transitions, vote
summary, and opaque diagnostic signatures. It never includes expected answers,
memory bodies, deleted values, leak strings, raw evaluator reasoning, or raw
build output. The complete safe incident dossier remains a durable audit
artifact; the Architect receives a bounded view containing aggregate rule
counts and representative, valid trace IDs rather than duplicated full trace,
timeline, and allowlist arrays. Only one copy of the latest structured feedback
is included in the prompt.

Answer composition is downstream of a typed trusted decision. The renderer can
word an authorized answer, but cannot change its action or cite records outside
the authorized set. A contradiction produces a logged, sanitized conflict and
a deterministic fallback made only from authorized evidence.

Authorization is deny-first and policy-driven. The generic evaluator lives in
`templates/memory_system/policy.py`; domain roles and resource classifications
live in JSON fixtures under `templates/memory_system/policies/`. Isolation,
explicit denials/revocations, and inactive records precede ownership, explicit
grants, capabilities, groups, attributes, relationships, and role rules.

## Transactional ingestion and promotion

An unrepresentable incoming delta stays byte-for-byte staged. Failed candidates
cannot move the cursor, mutate the committed episode image, commit the boundary,
or expose a rejected workspace as the parent. Each candidate is tested against
that exact delta. Before promotion, the copied candidate workspace is checked
against its new provisional manifest, code hashes, component versions, and
fixed contract version. Verification failure rejects it while the parent
pointer and images remain unchanged. Promotion then atomically publishes migrated images, the committed
version pointer, the ingestion cursor, the boundary's ingested state, and the
resolved representation incident. A crash after that transaction resumes at
official prediction and cannot stage or ingest the delta twice. A wrong
official answer opens scored repair from this representation-capable committed
parent; it never rolls the data or parent back. After five failures,
pre-ingestion terminalization atomically advances only the scheduling cursor;
it does not publish a version, image, or ingestion cursor.

For scored failures, official rows are never rewritten. Candidate replay rows
live in the `repairs` table and `repair_predictions.jsonl`. A promoted scored
repair version only serves future official boundaries; the earlier
representation promotion is the frozen version for this boundary's official
rows.

`--replay-reingest` is a manifest-pinned semantic option. It rebuilds each side
from the authoritative ordered turns and committed ingestion map, allowing
ingestion and state-construction changes to be measured. A run created without
it cannot be resumed with it enabled. `--replay-votes` must be a positive odd
integer; use `--replay-votes 3` for live evaluation. Reports distinguish
unanimous/majority pass and fail from evaluator-error/inconclusive results, and
an inconclusive mandatory gate cannot promote.

Each immutable parent vote is cached by parent code/component versions,
checkpoint and boundary, logical state-image signature, visible checkpoint,
model/config identity, renderer mode, fixed contract version, vote index, and
deterministic seed metadata. Individual votes are journaled immediately and
independent votes run under a bounded semaphore. Renderer-only replay reuses one
frozen trusted memory decision. Any version, configuration, state, or checkpoint
change invalidates the key; broad validation can reuse an identical parent
baseline without replaying it.

## Repetition prevention

The runner first compares a normalized structured repair intent with rejected
attempts for the same
incident class and safe parent, including durable history from interrupted
runs. The intent contains component, failure stage/mechanism, target file and
function/artifact, change type, cited causal event/contract reason, and expected
observable. Rewording or moving the same prompt instruction is still the same
intent; a different implementation mechanism is permitted. It also records and
compares diagnosis, design/work-order, migration, source
diff, and behavioral signatures alongside evidence, action/answer, replay
scores, and failed targets. An equivalent proposal can skip Developer work; an
equivalent source diff or replay behavior skips unnecessary downstream work.
The rejection is `equivalent_previous_attempt`, identifies the duplicated
attempt, returns that fact to the Architect, and remains at the same boundary.
Duplicates are rejected before Developer or replay. Exact source-diff and
post-replay behavioral signatures remain additional safeguards.

## Rejection feedback and diagnostics

Structured feedback follows the stage precedence above and distinguishes no material code change, duplicate
proposal/source/replay behavior, build or test failure, ineffective design,
safety regression, and infrastructure failure. It includes the objective,
trigger, safe parent, attempted diagnosis and work order, files changed, exact
build/test errors, gate statuses, targeted and broad results, separate
parent/candidate vote summaries, before/after selected and required claims,
contract reason enums, fallback use, answer source, action categories, rendered
answer hashes, authorized evidence, decision traces, transitions, remaining
failures, and safety/preservation/isolation regressions,
signatures, token/time use, previous approaches, and the exact rejection reason.
The newest complete feedback record is paired with compact summaries of older
attempts, keeping retry context bounded. Hidden expected answers, judge
specifications/reason strings, leak targets, gold fields, labels, and future
data are stripped before every diagnostic boundary.

INFO logs report incident, candidate, phase, focused goal, build result,
targeted result, broad result, behavioral changes, remaining failures, reason,
and action. If replay never ran, `failed_remaining=unknown`; absence of replay
data is never reported as zero failures.

## Durable artifacts and recovery

```text
runs/<run_id>/
  run_manifest.json
  event_schedule.json
  checkpoints.sqlite
  cumulative_scoreboard.json
  official_predictions.jsonl
  repair_predictions.jsonl
  versions/version_0001/{workspace,manifest.json,parent.json,promotion_report.json}
  events/boundary_0001/
    input_delta.json
    compatibility.json
    frozen_version.json
    boundary_outcome.json
    repair_incident.json
    architect_incident_dossier.json
    attempts/attempt_01/
      design.json
      diagnosis_review.json
      developer_outcome.json
      build_validation.json
      targeted_replay.json
      broad_validation.json
      structured_feedback.json
      validation.json
      workspace/
```

SQLite is authoritative. Event rows and repair incidents durably hold
`failed_candidate_attempts`, the invalid-proposal counter,
`representation_committed`, `representation_committed_version`, the exact
staged-input digest, `schedule_advanced`, `ingestion_occurred`, and the terminal
boundary outcome. `boundary_outcome.json` materializes the same boundary status.
Official table triggers prohibit prediction, score, and row
replacement or deletion. JSON files are reproducible materialized views. On
resume, completed official predictions and ingestion are not repeated; an
unfinished `building` attempt becomes `interrupted`, never promoted. The same
active incident, boundary, safe parent, staged delta, counters, and unresolved
target set resume. Legacy manifests containing `max_stalled_attempts` or
`max_duplicate_proposals` are accepted; those fields are recorded as ignored
legacy limits and replaced with the new defaults/options.
The old environment names `ONLINE_MAX_STALLED_ATTEMPTS` and
`ONLINE_MAX_DUPLICATE_PROPOSALS` remain fallback aliases when the corresponding
new environment variable is unset.

Legacy answer-plan reconstruction is limited to artifacts with no answer plan
or an explicit legacy marker/version. A current schema with missing, malformed,
or empty claims fails plan validation as `FACT_SELECTION`; it is never silently
expanded to all authorized evidence. Escalation records remain in the durable
operator queue. A later promotion of the related component marks them eligible
for reconsideration, and the summary warns when
`ONLINE_ESCALATION_RATE_WARN` is reached.

Exactly-once applies to committed ingestion and official rows. A crash after an
external model response but before journaling can repeat that unrecorded remote
request and its billing. Historical memory images are experiment archives; a
deployment-wide erasure policy would need a separate archive deletion or
encryption policy.
