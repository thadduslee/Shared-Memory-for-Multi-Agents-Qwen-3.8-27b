# t114 coverage and supervised recovery

The v8 run stopped at boundary 7 after seven committed predictions and 113 complete
source jobs. All three t114 attempts proposed the same assignment and bindings
for one recipient while reporting `changed` coverage for both that recipient and
the authorizing subject. The host rejected the subject's label because no
recipient activity/assignment changed for them. The generic rejection feedback
did not identify that distinction; the two repair requests were identical.

## Changes

- The host now derives diagnostic coverage labels from operations that have
  already passed all authority, scope, evidence, temporal and binding checks.
  Every required principal and live evidence remain mandatory. Adjustments and
  the original model coverage are retained in permission attempt validation.
  No operation is added, removed or made valid by a coverage adjustment.
- Permission correction inputs include an attempt number and explicit repair
  guidance to avoid resending an identical rejected prompt indefinitely.
- The evaluation supervises known permission, transport and model-contract
  failures. It retries the same stage with 5–30 second backoff, logs failures and
  writes recovery status/history. Sources and queries cannot advance past a
  blocked stage. Persistent model/configuration problems can remain visibly
  blocked; this is not a guarantee that every future failure can self-repair.
  Access denials, programming errors and Ctrl+C are not swallowed.
- Pending sources recover at their original observed timestamps before replay
  moves the fixture clock through earlier completed sources.
- Evolution transport failures no longer consume the bounded contract-rejection
  budget. Audited attempt IDs remain monotonic and resumed repairs retain the
  last validator feedback. Repeated invalid proposals are still rejected after
  the configured limit. Answer response tickets are discarded because answers
  use the final release barrier, avoiding retained failed responses on retries.
- HTTP failures include a bounded server reason instead of only the status code.

## Evidence

- `replay_failure.py` replays the exact saved rejected t114 proposal on copied
  databases. It now passes with its operations unchanged; only the subject's
  coverage label is adjusted. The original run is not modified by this replay.
  See `replay-report.json`.
- All **178 security tests** and **42 evaluation tests** pass. Regressions cover
  forged authority, missing coverage principals, derived change labels, pending
  clock recovery, source ordering while retries block, visible interruptible
  recovery, strict manifest checks and code-revision lineage, repeated evolution
  outages and bounded invalid evolution proposals.
- An offline run starts with two boundaries, then resumes for one more. Its
  final state contains three complete boundaries, 83 turns and four scored
  original predictions. This is a functional check, not model-quality evidence.
- Both local model health endpoints return HTTP 200 before real resumption.
- Before edits, all three databases and the original implementation source are
  backed up here. `before.json` preserves seven predictions/scores, 113 completed
  job hashes/proposals/changes and the immutable original manifest. The revised
  source snapshot and its code digest are recorded separately.

## Experiment lineage

This continuation intentionally changes evaluation code. The original code digest
is `90224dfcdd1ddedd1ee7b2ecdee6ed08c31ccea7bec335d024d8f9bf7a8256c3`.
The revised digest is
`d3992bb1aa56a3738eac240848cf09b510a3083e1e82f4b488a68443b664d47f`.
The runner receives the former through `--resume-code-revision`, verifies that
only code changed in the manifest, and records the revision after the seven saved
predictions. The original manifest, predictions and scores remain immutable.
The summary labels this as a mixed-implementation run. A clean paired benchmark
would use a fresh run directory; the requested continuation retains completed work.

The real evaluation resumes through `resume-resilient.sh` in the same
`amem-v7-full` pane `%7`. Later interruptions can use the run's ordinary
`resume.sh`; it now accepts the recorded active implementation and automatically
recovers pending sources. The one-time upgrade script is not needed again.

## Live result

The real t114 retry completed after one new model call (four attempts including
the three previous rejections). The host derived the subject's coverage label,
and its assignment and two memory fields published through the normal validators.
The runner replayed t113/t114 idempotently, completed t114 evolution, and started
t115 in the original pane. `preserved-work.json` verifies all 113 earlier complete
jobs, all seven predictions/scores, and the original manifest are unchanged.
The effective manifest matches the tested revision; the run remains active.
