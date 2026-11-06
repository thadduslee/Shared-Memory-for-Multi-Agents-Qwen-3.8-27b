# Exact quote/source pairing — 29 October 2026

The run stopped at t030 after 29 complete source jobs. All three rejected
responses copied the allergy statement from t026 while selecting the source ID
of t030's lab-draw statement. The quote was real but belonged to another source.
The host correctly rejected it; generic feedback did not identify the actual
source and the model repeated the same invalid binding.

## Changes

The response schema now pairs each bindable source ID with that source's own
exact quote choices. Quote selection precedes source selection in decoder order.
An independent combination of a quote from one turn with another turn's ID is
not a schema-valid model response. Choices are generic asserted sentences,
semicolon/newline clauses and already-retained exact field spans. They are text
selectors, not permissions. Original scope, necessity, subject, authority,
context/metadata and final restriction assessments remain independently required.

For arbitrary finer excerpts, the learner can request bounded exact-quote
retrieval using the existing causal-prefix context mechanism. Matches add the
source and excerpt to subsequent choices. Literal quote text is never rewritten
as a source alias. Source isolation, liveness, three-round retrieval budgets and
atomic publication remain enforced. Questions and ambiguous repeated quotes are
not offered as factual choices. Canonical callers may still provide arbitrary
exact spans/unique quotes, with the same host checks as before.

The host's independent quote error now identifies the operation path, supplied
source, actual matching source IDs, available choices and any required retrieval.
It never reassigns evidence or accepts a fabricated quote automatically.
Snapshot acknowledgment arrays (stored restriction IDs and reviewed recipients)
are fixed to their known values in the decoder; actual semantic decisions remain
model output subject to host validation.

## Verification

**215 security tests and 53 evaluator tests pass.** The added scenarios cover
wrong-source pairs in the actual JSON schema, fine historical excerpts, question
exclusion, repeated phrases, precise repair feedback, atomic quote retrieval,
nonexistent quotes, literal alias words, persistent retrieval budgets and fixed
snapshot acknowledgments. Existing security coverage remains intact.

The local live-model probe opens copies of the failed prefix, keeping the
original stores unchanged. Both probes completed the pending job in one model
call through normal host validation. The final probe took 22.39 seconds with
13,486 input / 565 output tokens and selected the actual lab-draw statement from
t030. The complete output passes independent JSON Schema validation. Replacing
its quote with the saved allergy statement makes the response fail that same
schema; selecting the actual allergy source is structurally valid but still
requires separate permission validation. See `live-probe-final/report.json` and
`live-probe-final/pair-check.json`.

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
git diff --check
```

These are focused checks, not a full-run quality or speed claim. The constrained
choices do not guarantee that every future semantic permission judgment succeeds.

## Continuation

Both original stores and the evaluation journal were backed up before editing.
`before.json` retains hashes for all 29 complete jobs and all 35 attempt rows,
plus the original manifest hash. Prior attempts and counters are not reset.

Previous active implementation:
`d44e9d4aecc2bfe7d388ea22ca6ce79ab0a88a23085aff0de80fd9c74ffb1065`

Reviewed implementation:
`b496d33757cf20f067432a96d456e818d88c9c3115c0c6ce5c4e5dde5a576862`

The one-time `resume-reviewed-quote-repair.sh` records the revision, then permits
at most two further proposals for the specific failed job using the existing
trusted five-total-rejection limit. It uses the run lock and normal validator;
no permission operation is silently removed or made valid. Successful repair is
recorded through an appended recovery event, followed by the ordinary uncapped
resume in the original tmux session. The immutable original manifest stays
preserved and the run continues to report `mixed_implementation: true`.

## Live continuation

The original t030 job succeeded after one new model call (attempt 4). The run
then completed t031 through t054 with one attempt per turn, saved and scored
the first checkpoint, applied the first four-field enrichment batch, and began
boundary 2. At the verification snapshot t055 had also completed in one attempt.
`preserved-work.json` confirms all 29 earlier complete jobs, all 35 earlier
attempt rows and the original manifest are unchanged. The original tmux session
continues the uncapped 579-checkpoint / 567-boundary schedule.

The structural quote audit examined all 4,294 source turns and 5,695 offered
excerpts. Every offered excerpt was exact, unique within its source and outside
question spans. It made no model calls and did not expose future turns or
evaluation labels to the running agent. See `full-source-quote-audit.json`.
