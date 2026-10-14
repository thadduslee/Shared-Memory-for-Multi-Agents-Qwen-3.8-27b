# Online GateMem memory evolution

Online mode is a durable prequential experiment: ingest, predict, persist the
official first attempt, judge it, and block at that boundary if repair is
required. Official predictions and scores are immutable. Repair predictions are
stored separately and promoted code affects only later official predictions.
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

- `--max-boundaries N`: commit at most N additional boundaries in this
  invocation. It never permits skipping an unresolved boundary.
- `--replay-window N`: cap the recent visible outcomes included in Architect
  context. Mandatory broad replay still covers every observed scored result.
- `--utility-regression-tolerance F`: maximum decline among previously passing
  utility cases. Privacy and deletion regressions are always rejected.
- `--checkpointer-path PATH`: address the authoritative SQLite journal
  directly. With `--resume`, its manifest determines the artifact directory.
- `--retry-blocked`: after the operator fixes an external infrastructure
  problem, or after a stalled incident, retry the same durably blocked boundary
  with the stall counters reset.
- `--abandon-blocked`: record a stalled scored incident's targets as unrepaired
  (their official failures stand) and advance the stream. A staged delta that
  cannot be ingested cannot be abandoned.
- `--disable-online-redesign`: run compatible boundaries without redesign and
  durably block, without advancing, when a repair incident occurs.

The online repair loop has no token, wall-clock, per-generation or
Developer-turn limit. Token and elapsed-time use remain recorded. It does stop
when it stalls: after `ONLINE_MAX_STALLED_ATTEMPTS` (5) consecutive rejected
candidates without a promotion, after `ONLINE_MAX_DUPLICATE_PROPOSALS` (3)
rejections for duplicating earlier work, or when the Architect returns
`NO_VIABLE_REPAIR`, the incident moves to phase `stalled` and the run to
`repair_blocked` without advancing. A candidate whose build still fails after
`ONLINE_MAX_CORRECTION_ROUNDS` (3) Developer corrections is rejected as
`implementation_failure`. Provider
request/context limits still apply, and transport retries remain finite so a
frozen request cannot hang forever. Exhausted transport retries produce
`infrastructure_blocked`; they do not become design feedback and do not advance
the boundary.

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

## Blocking state machine

The stream order is `episode-id-then-turn-position-v1`: episode IDs sort
lexically, turns use their recorded array position, and checkpoint IDs only
break ties within a boundary. No future checkpoint or hidden annotation is
used for scheduling or repair.

```text
stage exact incoming delta
  -> committed-version ingestion probe
  -> if incompatible/unrepresentable: blocking pre-ingestion incident
  -> atomically commit image + cursor when safe
  -> freeze committed version
  -> persist every official prediction at the boundary
  -> persist every official score
  -> if any score fails: blocking scored-checkpoint incident
  -> mark boundary done and advance only after the incident is resolved
```

Each durable incident records its ID, triggering boundary and type, exact
staged delta and digest when applicable, target checkpoint IDs, unresolved
targets, safe parent, candidate and feedback histories, signatures, current
phase, and promotion history. Its explicit resumable phases are:

```text
architect -> diagnosis_review -> developing -> build_validation -> targeted_replay
          -> broad_validation -> promotion
          -> rejected_with_feedback -> architect
          -> infrastructure_blocked
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

One proposal creates one isolated workspace from the safe committed parent.
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
  candidate image and verifies that each turn is represented.

Paired scored results retain evidence record IDs and content hashes, final
action, rendered answer, score, pass/fail, evidence/answer/score changes, and
failing-to-passing or passing-to-failing transitions. If no previously failing
target passes, broad replay is skipped and the rejection reason is
`no_measurable_improvement`.

Only a targeted gain opens broad validation. Mandatory broad gates cover:

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

A scored candidate is eligible only when it fixes at least one failed target,
does not turn any target from passing to failing, and every broad gate passes.
Safe partial progress may be promoted; the incident immediately continues at
the same boundary with the promoted version as safe parent until all targets
are resolved.

## Transactional ingestion and promotion

An unrepresentable incoming delta stays byte-for-byte staged. Failed candidates
cannot move the cursor, mutate the committed episode image, commit the boundary,
or expose a rejected workspace as the parent. Each candidate is tested against
that exact delta. Promotion atomically publishes migrated images, the committed
version pointer, the ingestion cursor, and the boundary's ingested state.

For scored failures, official rows are never rewritten. Candidate replay rows
live in the `repairs` table and `repair_predictions.jsonl`. A promoted version
only serves future official boundaries.

## Repetition prevention

The runner compares each proposal with all rejected attempts for the same
incident class and safe parent, including durable history from interrupted
runs. It records and compares diagnosis, design/work-order, migration, source
diff, and behavioral signatures alongside evidence, action/answer, replay
scores, and failed targets. An equivalent proposal can skip Developer work; an
equivalent source diff or replay behavior skips unnecessary downstream work.
The rejection is `equivalent_previous_attempt`, identifies the duplicated
attempt, returns that fact to the Architect, and remains at the same boundary.
Duplicates count toward the stall limits above.

## Rejection feedback and diagnostics

Structured feedback distinguishes no material code change, duplicate
proposal/source/replay behavior, build or test failure, ineffective design,
safety regression, and infrastructure failure. It includes the objective,
trigger, safe parent, attempted diagnosis and work order, files changed, exact
build/test errors, gate statuses, targeted and broad results, before/after
action, rendered answer, authorized evidence, decision traces, transitions, remaining
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

SQLite is authoritative. Official table triggers prohibit prediction, score,
and row replacement or deletion. JSON files are reproducible materialized
views. On resume, completed official predictions and ingestion are not repeated;
an unfinished `building` attempt becomes `interrupted`, never promoted. The
same active incident, boundary, safe parent, staged delta, and unresolved target
set resume. The repair loop continues before the stream can advance.

Exactly-once applies to committed ingestion and official rows. A crash after an
external model response but before journaling can repeat that unrecorded remote
request and its billing. Historical memory images are experiment archives; a
deployment-wide erasure policy would need a separate archive deletion or
encryption policy.
