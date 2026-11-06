# Version 7 implementation and validation

Implemented changes are described in [GOVERNED_AUTHORIZATION.md](../../GOVERNED_AUTHORIZATION.md).

- Generic capability candidates and protected approval/revocation feedback are
  separate from runtime assignments. Stable candidate IDs cannot cross issuer,
  principal, or selector boundaries. The learner cannot activate permissions.
- Recipient coverage and explicit original-authorization references make missing
  staff decisions visible and make rejected bindings repairable.
- A configured mediated-read convention fixes literal read-only grants that
  could not enter the assistant's search/read/derive pipeline. The kernel retains
  distinct actions; adapters without the convention remain read-only.
- Exact subject-bounded erasure removes readable fields, dependent artifacts,
  and indexes; persistent hashes block matching republication. Host-only source
  and audit history are retained. This is logical erasure, not physical wiping
  or a general semantic-paraphrase detector.
- Authorization reuses current permission state within managed transactions;
  SQL changes, rollback, new transactions, session changes, clocks, and delegated
  namespace limits are checked. Unmanaged transactions do not reuse the cache.
- Current-plan retrieval uses source sequence metadata and reserves recent
  authorized instructions. Explicit question sentences cannot be relabeled or
  cropped into asserted facts. Generated scope labels stay out of answer input.
- Answers distinguish denied access, missing memory, and partial disclosure.
  An evidence plan precedes the reply, with application-controlled labels and
  a read-only host scope review for ambiguous missing parts. Scope labels stay
  private; unsupported replies are replaced with generic responses.
  Utility judging evaluates individual requirements before the overall verdict;
  context flags receive a semantic review. Truncated responses fail after one
  request instead of repeating the same token-budget failure three times.
- Whole evolution/source timings expose neighbor-search cost; model-call time
  is recorded separately from permission validation and publication.

## Automated checks

`verify.py` runs the security and evaluator suites, profiles a disposable v6
copy, verifies retained live-model results, checks the diff, hashes runtime code,
and confirms original benchmark/run artifacts are unchanged. Its results and
test counts are in [validation.json](validation.json), with full logs beside it.

The checks cover cache invalidation and decision equivalence, delegated scope
attenuation, authority ownership, literal versus mediated reads, exact field
boundaries, current-plan conditions, immediate/future erasure, numeric identifier
formatting, republication, atomic rollback, response labels, and judge coverage.

## Measured search cost

[performance.json](performance.json) measures a full authorized search over all
448 v6 indexed candidates on a disposable database copy. It completes in about
two seconds. Stored vectors isolate authorization/search; there are no embedding
or model calls. Cached and uncached decisions match for all 27 sampled checks.
This is not an end-to-end boundary speedup measurement.

## Real-model checks

The initial [release-coordination development stream](real-smoke-1/report.json)
confirmed task acceptance for a reviewer, operational contact access, and erasure
for both the owner and coordinator. Its logistics check failed because the model
had granted read without search/derive. That failed prediction is preserved.
The development stream was resumed after improving binding-repair feedback;
archived checkpoint probes were not replayed against future state.

After adding the explicit mediated-read contract, a separate fresh
[logistics regression](real-logistics-2/report.json) passed all five checks:
current time, room, cancellation, exclusion of sensitive context, and withholding
of the protected callback/rollout request. The same core/learner/governor were
used with a nonmedical source stream; this is a focused diagnostic rather than a
second-domain benchmark or proof of generalization.

The revised independent judge re-evaluated saved v6 cases separately in
[judge-review.jsonl](judge-review.jsonl). It correctly rejected incomplete answers
02, 03, and 06 and cleared checkpoint 12's context false positive, retaining its
action mismatch. The original v6 predictions, scores, summary, and manifest were
not modified. An earlier response-format inspection is retained for debugging;
the prompt and schema were aligned so item coverage precedes overall utility.

[response-plan.json](response-plan.json) checks the completed three-source
logistics prefix with the final response contract. The permitted request returns
answer, the protected request returns refuse, and the mixed request returns
answer_redacted. No private callback value appears in any response or released
context. This read-only review adds no source jobs or permission decisions.

## What remains unmeasured

The full GateMem benchmark has not been rerun under v7. These changes establish
implementation and focused behavioral checks, not a new official accuracy or
leakage rate. Source interpretation, candidate completeness, and semantic judging
remain fallible. Model generation still takes tens of seconds for substantial
permission proposals, even after removing the repeated-ledger bottleneck.

The learner now has protected authority feedback and an explicit proposal
interface; automatic training or cross-domain policy promotion is not enabled.
Those require separate approved feedback and held-out evaluation protocols.

Start a fresh official run as documented in the root guide. Do not resume v6
under changed code/prompt/bootstrap manifests.
