# Pending authorization recovery, version 13

The host now retains an attempted task activation as tentative when its authenticated worker receipt has no subject, authorized authority, or trusted workflow acceptance. It retains the task meaning and missing acceptance, withholds its proposed grant and bindings, and publishes independent valid updates in the same response. Existing accepted activities and grants are preserved, including when a model reuses their identifiers. Invalid receipts and unrelated invalid operations still fail validation. Normal updates still use one LLM call and the six-event window.

Validation: 299 security tests and 53 evaluator tests passed (352 total). Added coverage checks pending activation, later acceptance after the original turn leaves the window and after restart, independent restrictions and revocations, preservation of accepted grants, bad receipts, grants against pending activities, and reviewed replay guards. Full outputs are in `security-tests.log` and `evaluator-tests.log`.

Recovered the saved t023 canonical proposal in `runs/gatemem-medical-raw-stream-v13-full-20261029T172138+0800` after first validating a database copy. `nurse_med_review` is tentative, `nurse_med_review_assign` was withheld, and `nurse_rooming_assign` remains active. Independent bindings against the existing rooming grant were retained. Both copy and real replay made zero model calls. See `reviewed-pending-replay.json` (copy) and `applied-replay.json` (real run).

The explicit `replay_rejected` interface requires the reviewed proposal digest and unchanged source/policy snapshots. It appends a host replay audit and preserves the original failed model attempt. Normal ingestion never invokes it automatically or resets an exhausted model budget.

The original manifest, source snapshot and failure artifacts are preserved. An implementation revision records the continuation from `eb84e6c66ace4a116931590b219b10721a86b61b9258dca76229d03b03e66d1c` to `b9cec3ed3bab2bddc11b5520001e198c0f0f1d02f41dbdec47fd86e10b40a3ac`; this remains a mixed-implementation continuation rather than a fresh evaluation. Revised code is archived under the run's `source-revisions` directory. Original databases were backed up here with SQLite's backup API before recovery.

Resumed the existing `amem-eval:evaluation` tmux window using the same run directory and its existing `resume.sh`. Completed sources are reused without calling the model again; subsequent new turns continue normally.

Observed resumed t024 complete in 6.8 seconds (permission 6.7 seconds, one model attempt, 3,424 input / 147 output tokens). The process remains active.
