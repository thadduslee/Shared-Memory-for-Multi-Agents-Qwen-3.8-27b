# Restriction authority recovery, version 13

Authority-bearing single-pass deltas now choose `authority_source`; the host derives the issuer from the authenticated source speaker and checks their configured capability and resource boundary. The prompt explicitly preserves existing policy for confirmation questions. The compact current policy still excludes evidence and the dialogue window remains six events.

A proposed restriction without authenticated restriction authority is retained as `status: unresolved`, with no authenticated issuer. Its proposed attribution remains in host audit. Independent valid operations publish without a second model call. Existing accepted restrictions cannot be superseded by an unresolved proposal, and existing grants are not rewritten by that proposal.

Until an authority answers, a runtime hold blocks the named recipients/actions for the subject, through both old and new grants. This is conservative: arbitrary semantic scope descriptions cannot safely narrow that hold in host code. Other recipients and subjects remain unaffected. An authority can later resolve the hold without adding a limit, or confirm a restriction using the existing supersession mechanism. Resolution requires a later authenticated answer, cannot remove accepted restrictions, and loses effect if its receipt is deleted. Stored grants do not automatically widen when a hold clears.

Added security coverage exercises derived speaker identity, unresolved policy projections, old/new grant enforcement, unchanged accepted restrictions, authority resolution after the dialogue leaves the window and after restart, unauthorized and stale replies, authorized supersession, and deleted resolution evidence. See the saved test logs.

Replayed the exact saved t027 canonical proposal on a database copy with zero model calls. It now completes with `elena_linda_med_scope` unresolved and the original `elena_linda_scope` intact. `nurse_med_review` remains tentative and its new grant is withheld; the existing nurse rooming grant remains active. The host replay preserves the failed attempt and requires unchanged source and policy snapshots. The original run has not been reset.

The original manifest, prior source snapshots and failure artifacts are preserved. Pristine pre-recovery database backups have `before-` prefixes in this directory. The revised implementation is archived under the run's `source-revisions` directory. Continuation is explicitly recorded as another implementation revision in the same run, rather than reported as a fresh evaluation.

Validation passed: 311 security tests and 53 evaluator tests (364 total). Applied the same reviewed t027 replay to the original run, with zero model calls; see `applied-replay.json`. The original manifest is unchanged. Code revision: `b9cec3ed3bab2bddc11b5520001e198c0f0f1d02f41dbdec47fd86e10b40a3ac` to `2a3e028619f255ef28699115a9e59c914f222ed59bdb625b35c42baae694ae68`.

Resumed `amem-eval:evaluation` with the same run directory. Observed t028 complete in 10.7 seconds with one permission model attempt (3,711 input / 223 output tokens), followed by t029 running.
