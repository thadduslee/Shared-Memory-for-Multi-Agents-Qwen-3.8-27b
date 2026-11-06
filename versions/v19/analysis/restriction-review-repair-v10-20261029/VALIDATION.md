# Restriction review contract repair — 29 October 2026

The first real-model v10 run stopped at t008 after seven completed source jobs.
Attempt 1 omitted a newly proposed restriction from its binding assessments.
The validator correctly rejected it. Attempts 2 and 3 added the required
assessment but also added that new restriction ID to `considered_restrictions`.
The host required that acknowledgment list to equal the **stored** restriction
set exactly, while binding checks required the **final** restriction set.
Generic feedback obscured this distinction and rejected the repaired responses.

The earlier tests verified final-state binding checks, but did not include this
full repair path in which the model updates both restriction lists. Their passing
result did not establish real-model repair reliability.

The host now requires all stored IDs in the acknowledgment and permits additional
IDs only when valid restriction operations actually create them in this proposal.
Unknown/duplicate IDs remain invalid. Each binding still needs an explicit
applicability decision for every final restriction. Missing acknowledgments
include their path and required IDs in repair feedback. No applicability decision
or authorization is filled in by this change.

## Evidence

All **206 security tests and 53 evaluator tests pass**. The added regressions
cover the two-list repair path, valid new IDs in every operation order, missing
stored restrictions, invented/duplicate IDs and unauthorized restriction issuers.
`git diff --check` passes.

`replay_saved_failure.py` opens the backed-up databases read-only and checks the
three saved proposals without a model call or publication. Attempt 1 remains
rejected; attempts 2 and 3 pass with their operation types/counts unchanged.
See `replay-report.json`.

## Audited continuation

Before editing, both memory stores, the evaluation journal, failed-run metadata,
source files and hashes of all seven complete jobs and ten attempts were saved
in this directory. The original run manifest and completed work are retained.

Original implementation:
`b93f430cbd088310576155ace27746bd41e2830e4bba47a2356388d89515935d`

Revised implementation:
`d44e9d4aecc2bfe7d388ea22ca6ce79ab0a88a23085aff0de80fd9c74ffb1065`

`resume-reviewed-repair.sh` first acknowledges the prior digest through the
runner's code-revision interface without processing any new boundary. The
bounded repair helper then uses the existing trusted `max_attempts=5` interface
for this specific job: at most two further model proposals beyond the three
already rejected. It records that allowance, acquires the run lock, retains
every attempt, and applies the normal validator/publication path. No job status,
proposal or rejection counter is deleted/reset. A successful repair clears the
stale recovery status through an appended audited recovery event, then the
ordinary uncapped resume runs in the same tmux session. Failure stops the repair.

The original manifest remains immutable; the runner records the new code digest
in `effective_manifest.json` and `implementation_revisions.json` and labels the
continuation `mixed_implementation: true`. This continuation is not a clean
fixed-implementation comparison.

## Live continuation result

The reviewed t008 retry succeeded after **one new model call** (attempt 4).
The ordinary full resume is processing later sources; t009 completed. The
`reviewed-repair-result.json` in the run records the outcome. `preserved-work.json`
verifies all seven earlier complete job rows, all ten prior attempt rows and the
original manifest are unchanged. Normal subsequent resumes use `resume.sh`;
the one-time reviewed-repair wrapper must not be rerun after its acknowledged
code revision has already been recorded.
