# V17 implementation and verification

Status update: the v17 real run failed the requested utility/latency objective
and was stopped with its artifacts preserved. See [V18.md](V18.md) and
v18-diagnosis.json for the observed failures and corrective verification.
The original launch record below describes that failed v17 run, not a current
success or a completed full evaluation.

The user explicitly approved
sending the evaluation through OpenRouter and requested that it start in tmux.
The fresh full real-model run started at 23:11:36 Singapore time on November 1,
2026, in session `gatemem-v17-full-20261101T231136+0800`, with `evaluation` and
`results` windows. Run artifacts are in
`runs/gatemem-medical-v17-openrouter-full-20261101T231136+0800`.
The runner and supervisor were confirmed live, and frozen source matched every
corresponding digest from the passing tests. The run uses JSON transport with the
same local grammar/canonical checks and prefers ModelRun for the configured Qwen
model. Full completion and improved live accuracy are not yet claimed.

Attach with `tmux attach -t gatemem-v17-full-20261101T231136+0800`.
The earlier automatic approval rejection was resolved by explicit user approval;
no network workaround was used.

## Requirements and current evidence

1. Correct recipient access: explicit per-segment, multi-effect intents; actual
   worker receipt and current authority statement; closed necessary worker fields;
   read-only service context; explicit speaker-versus-information-subject attribution.
2. Separate communication, disclosure, revocation and forgetting: communication
   preferences do not mutate ACLs; source intent and operations must agree; recipient
   scope cannot silently change during restriction replacement.
3. Avoid repeated historical work: exact-edge updates, reuse of existing memory
   views/vectors/approved targets, one publication lookup per candidate/source,
   batched final-state validation, and fingerprint construction without copying
   binding histories. Unrelated deletion receipts are retained.
4. Active forgetting: exact value hashes, dependency closure/tombstones, same-source
   field isolation, anti-resurrection checks, and deferred timestamp/conditional
   requests with current evidence and a trusted host sweep API.
5. Useful answers: selected authorized source instructions are retained in user
   output with their qualifiers; guessed model prose is excluded. Final release
   still rechecks every consumed input. Missing facts remain distinct from denial.
6. Measurements: current pending reviews versus repeated historical observations;
   scalar timing survives erasure; model versus other time and early/late latency;
   native scoring unchanged. Both provider grammar and identical local validation
   are available. Rejected JSON drafts are protected repair data, never executable.

The source model no longer chooses new bookkeeping IDs: it selects `new`, and the
host assigns unique IDs by source job/operation. Intent and fact classification
use required named slots to prevent duplicated/missing indexes.
Every fact-classification slot now has one complete row shape, avoiding accidental
selection of a short context branch for an instruction. Empty optional supersession
is explicit and cannot replace another restriction.

The application adapter also runs at most one trusted maintenance batch after
each source event. This schedule is independent of checkpoint placement and
requester queries. It uses the v17 exact-pair format, original authority receipts
and the same canonical checks. The extra model work is outside the two-response
update budget; its duration is included in source_total and itemized separately.

## Deterministic evidence collected

- Original baseline: 497 tests plus 43 subtests passed.
- Latest verified source: 514 tests plus 43 subtests passed, no failures/skips,
  in 112.17 seconds. See verification-20261101T141229Z/regression.xml and
  regression.log. All recorded source digests matched again after the suite.
  The fresh tmux session is gatemem-v17-20261101T141229Z.
- V17 regression-v17-final.xml: 510 tests plus 43 subtests passed. Subsequent wire
  refinements require another final frozen verification; see later regression files.
- Scoped deletion, future/conditional forgetting, unauthorized completion, live
  revocation after cache retention, source-backed answer details, schema validation,
  provider preferences and recipient grant checks have dedicated regressions in
  security_tests/test_selective_memory_v17.py.
- The development offline full schedule reached all 579 scored checkpoints,
  4,294 source turns and 567 boundaries. This proves runner coverage only, not
  semantic model quality; its processors deliberately create no permissions.
- The latest source was then rechecked in the fresh tmux session: the complete
  offline schedule again finished all 579 checkpoints, 4,294 source turns and
  567 boundaries, with exact coverage and no changed source digests. The test
  pane reports exit 0; the offline summary reports complete=true (its pane closed
  on completion, so no separate shell exit receipt was retained). See
  verification-20261101T141229Z/offline-full/summary.json
  and local-requirement-audit.json. This remains scripted runner verification,
  not live semantic quality evidence.
- Identical-decision host benchmark: 60 sources, five readers, 60 readable records,
  no pending reviews in both versions. Last-25 mean fell from 4.111059743 s to
  0.431888748 s (9.52x). This excludes API time and is not a full dataset latency claim.
- Repeating that host benchmark after the latest format/maintenance fixes gave
  0.493048526 s for the last 25 sources, with the same 60 readable records and
  zero pending reviews (8.34x versus the saved baseline). See host-v17-60-final.
  This benchmark measures update processing with scripted decisions, not the
  application's additional maintenance schedule or external model inference.

## Development findings retained

All failed probe directories are preserved. They exposed unnecessary holds for
ordinary offers, duplicate indexes, confusing model-selected ID pools, instructions
attributed to their speaker, overly narrow operational scopes, incomplete recipient
reviews, invalid structured output, and compound grant/deny intent mismatches.
Fixes and subsequent verification must remain distinguishable from these failures.
The final live probe and requirement audit are pending.
The last completed live attempt (live-13-json) failed: an incomplete instruction
row consumed the repair budget at source 2, leaving initial service facts pending;
later a null optional supersession field was rejected twice. Both causes have
local fixes and regressions, but those fixes have not passed a subsequent live probe.

## Fresh evaluation contract

The new supervisor freezes source without credentials and runs all native
checkpoints/source tails with the same embedding, Qwen memory/answer model and
GPT-4.1 judge as v16. No label supplies a permission, no score changes inference,
and no scored failure stops the requested full schedule. Original v16 artifacts
are preserved. Completion requires actual final coverage and inspected metrics,
not a tmux launch or an offline run.

Protected source/audit retention follows logical erasure; external provider
retention and physical backup erasure are outside the runtime. Natural-language
authority, attribution and relevance judgments remain fallible and require real
evaluation. No zero-error or full-quality claim is established by unit tests.
