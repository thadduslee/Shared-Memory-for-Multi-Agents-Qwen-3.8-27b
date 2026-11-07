# Single-pass permission updates, 29 October 2026

V13 replaces mandatory candidate reviews, recipient coverage, historical lookup,
focused reassessment and semantic repair loops with one decision per source turn.
The model sees at most six recent events and concise committed policy, including
tentative task intent. A no-op is `{"changed":false}`; an update contains only
changed operations. Original evidence and field history remain in protected audit
storage. The model does not receive those histories as permission context.

New grants include their initial exact fact bindings. Later facts bind to an
existing active grant. A pending offer stores worker identity, task meaning,
possible subject and unresolved acceptance without creating access. Acceptance
can resolve that same activity after the worker's original source leaves the
window; the host preserves and checks its authenticated receipt.

The model returns one shared short rationale and source list per operation.
Exhaustive `blocked_by` and `blocked_assignments` lists express restriction
overlap without repeated per-record essays. The host expands these explicit
decisions into the canonical audit format and suspends named overlapping grants.
It does not independently reinterpret their natural-language meaning. Exact
quotes, source liveness, authority, subject isolation, final-state restrictions,
validity, atomic publication and retrieval/release checks remain in force.

Invalid semantic proposals stop after one attempt, with access blocked. A new
process cannot automatically reset the budget. Service outages retain transport
recovery. The 3,072-token permission response budget is separate from answer and
enrichment budgets and is recorded in the run manifest.

## Validation

- Security regressions: 291 passed, including 17 single-pass regressions, in the
  fresh launch against its pinned source snapshot.
- Evaluator regressions: 53 passed. Legacy v12-specific tests select version 12
  explicitly; new tests exercise the default v13 behavior.
- New regressions cover one-call no-ops with pending state, delayed acceptance
  after eight intervening turns and updater reconstruction, new-task receipt
  reuse, exact quote rejection/question omission, grant-with-facts compilation, shared
  validity evidence, scope restrictions, revocation, evidence deletion, final
  restriction ordering, policy changes during inference, omission of prohibited
  bindings, and restrictions leaving unrelated recipients' grants unaffected.
- The complete offline schedule finished all 567 boundaries, 579 scored queries
  and 4,294 source turns with exact coverage. This run was started during
  development; its manifest pins that build. Its empty scripted permission
  processor validates scheduling/storage, not semantic permission quality.

## Final local-model smoke measurements

Both final smokes used the existing local Qwen model, isolated fresh databases
and HashEmbedder, with no official predictions or scores rewritten. They did not
run answering, judging or enrichment. Reports and wire responses are retained.

| Case | Result | Model calls | Model time | Output tokens |
| --- | --- | ---: | ---: | ---: |
| [Original first eight turns](live-prefix-08/report.json) | Eight turns complete, no pending jobs | 8 | 29.464 s | 693 |
| [Delayed pickup acceptance](live-delayed-05/report.json) | Eleven turns complete; acceptance and revocation checks pass | 11 | 19.988 s | 483 |

Unchanged original-prefix turns generated six tokens each and took 0.406–0.761
seconds. The two substantive changes took 8.388 and 13.283 seconds. The delayed
test checks that no early access exists, the pickup facts become readable after
acceptance outside the original window, diagnosis text is excluded, and revocation
removes access. Each completed turn has exactly one permission call.

For the same first seven raw source turns, the saved v12 trace used ten calls,
10,366 generated tokens and 405.055 seconds of model time. The final v13 smoke
used seven calls, 352 generated tokens and 16.181 seconds. These are different
permission workflows and can produce different policy states; this small,
warm-service comparison is not a claim of equal accuracy or whole-run speedup.
See [derived measurements](metrics.json) and the per-call JSONL artifacts.

## Development failures retained

Earlier isolated attempts remain intact. They exposed an unsupported decoder
keyword, invented expiry/grant references, mismatched restriction bookkeeping,
discarded pending offers and a grant without factual bindings. The final contract
removes the unsupported keyword, constrains existing-record references, expands
explicit overlap sets mechanically and places initial facts inside new grants.
The pending-intent instruction now explicitly requires saving an unconfirmed
specific offer. No automatic second model opinion was added.

The first full v13 launch stopped at t012 on inconsistent restriction targets:
the model named a grant outside the rule's recipient set and attempted to bind
text it explicitly marked prohibited. That attempt and its source snapshot are
preserved in `runs/gatemem-medical-raw-stream-v13-full-20261029T170355+0800`.
The final compiler applies recipient/subject/action selectors mechanically,
leaves unrelated grants unaffected, and omits model-declared prohibited
bindings. Unknown grant IDs and invalid quotes are still rejected. Two additional
regressions cover this behavior; it adds no inference or repair call.

The second launch passed that point and stopped at t019 when the model tried
to bind a question as a factual instruction. The final compiler omits question
text from factual projections while retaining it as source/task context and
recording `binding_omissions` in attempt validation. A regression verifies one
call, no guessed factual value, and preservation of earlier authorized fields.
Inexact/fabricated quotes and invalid control decisions still stop the run.

The model can still miss or misinterpret a natural-language change. Eliminating
mandatory reviews deliberately removes a second opportunity to catch such
omissions. Structural checks cannot prove correct semantic scope or complete
inference; full judged benchmark results remain necessary.

## Fresh full run

`runs/gatemem-medical-raw-stream-v13-full-20261029T172138+0800` contains the new
full-run launcher, resume script and a source snapshot with hashes. The old v12
run remains preserved. Attach with `tmux attach -t amem-eval`; the evaluation,
model and judge use separate windows. All 579 checkpoints and 567 boundaries
are scheduled without a cap, with normal answering, judging and enrichment.
