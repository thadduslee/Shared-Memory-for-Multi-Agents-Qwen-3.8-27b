# V16 full evaluation — launch validation

The requested evaluation is a fresh full 579-checkpoint run in tmux session
`gatemem-v16-full-1101`, under
`runs/gatemem-medical-v16-openrouter-full-20261101T171100+0800`.
See that directory's FOLLOW.md for logs and monitoring. Native scores remain
pending until the run reaches its first checkpoint. Full-run success is not claimed.

## Why the original run over-refused

The original 34-checkpoint snapshot had no readable evidence at any checkpoint:
31 outputs were refuse and three were no_memory. It had no runtime read
assignments, 43 quarantined source jobs and 62 active control holds. Utility
accuracy was 0% and over-refusal 100%. Recorded failures included 13 missing
worker receipts, seven output-budget failures and three null-list crashes.
This was primarily an ingestion/authorization failure before answer generation.
The original zero leakage therefore did not demonstrate useful selective access.

## Fixes in the evaluated implementation

Authenticated subjects retain control authority over their own information,
starting with empty requester grants. Current control proposals see only subjects
the speaker can control. Separate own-binding tables explicitly distinguish the
reader from the information subject, preventing reversed-grant substitution.
Literal field grants retain their exact source scope. Model-selected activities
require the actual worker receipt and subject acceptance; operational contact can
reach that accepted worker while a time/place-only helper remains restricted.
Workers describing somebody else's situation get no automatic self grant.

Other-recipient fields receive an independent focused review. Every selected
pair has a required indexed verdict slot; the host fixes its candidate/grant IDs.
Main-call binding work is limited to own-subject grants to avoid duplicate worker
reviews. Ordered wire keys preserve intended reasoning order even on providers
that sort JSON schema keys. Positive bindings are invalidated by new negative
reviews and stale fingerprints. Revocation, expiry, erasure, source liveness and
final release checks remain enforced. Query-time processing cannot create grants.

Scoped denials no longer suspend unrelated access. Nullable lists, expiry
operations, source output caps and authenticated observation receipts were fixed.
Answers review permitted evidence before enumerating current, continuing,
retired and conditional items. A confirmation or shorter restatement cannot
retire omitted full dates, cancellations or exceptions without explicit correction.
The source work budget remains one combined call plus at most one focused
repair/review, with bounded batches of 12 binding pairs and local routing.

## Validation and limitations

Prelaunch regression-v21-final.xml contains 540 passing test cases (497 tests
plus 43 subtests). The final rerun after the restatement wording change also passes all 540 cases
with zero failures/errors in 126.239 seconds (regression-v22-frozen.xml). Live synthetic checks cover contact authorization,
closed-field scope, denied private fields, actual revocation, own-access survival,
full schedule dates, later events and cancellation completeness. Probe artifacts:
contact-15-projected, project-22-final, fields-07-final and late-denial-04-final.
fields-06-final failed because a shorter confirmation incorrectly replaced a
fuller record; the correction and subsequent passing check are retained.
These are development checks, not official full-dataset scores or held-out proof.

The full run retains the original configured memory/answer model, embedding
model and judge. Provider routing uses latency sorting and ignores deepinfra,
coreweave and reka after observed development issues. Routing preferences are
recorded in the manifest; provider variation prevents attributing measured speed
solely to the algorithm. Full latency and quality must be assessed from this run.

## Evaluation protocol

Process all 21 episodes, 579 checkpoints, 567 boundaries and 4294 source turns,
including source-only tails. Official predictions and scores are immutable.
The implementation is frozen, source is archived without credentials, and each
source has at most two semantic responses. Scores are never used to grant access.
Utility misses and privacy/deletion leaks remain in the full evaluation. Following
the user's 2026-11-01 instruction, automatic score-based stopping is disabled.
The original stop at eight scores is retained in early-stop.json and invocation
history; the same frozen evaluated implementation resumes without new fixes.

A read-only progress window refreshes counts and native boundary metrics every
10 seconds. Once the first 25 native scores are exported, compare.py produces an
exact chronological comparison with the original v15 run, including all measured
source timers and no old resume-timer exclusion. Native summary.json remains the
source for full-run coverage and aggregate scores. Completion requires all 579
scores and all source boundaries; tmux launch alone is not benchmark completion.
