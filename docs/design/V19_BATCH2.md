# V19 Batch 2: intent-preserving permission repair and grounded task acceptance

Continue Batch 3 in `/home/thaddus/a-mem-v19-fixes`, branch
`v19-recipient-revocation`. Batch 2 starts at **ee4cfc1** (Batch 1).
Its implementation diff is `git diff ee4cfc1..HEAD`.
The verified historical V19 baseline remains `3d0bc0c`; Batch 1 is preserved.
The original checkout, uncommitted work, historical runs, and scores were not edited.
No V20/V21 workflow was imported. New behavior is gated on version family 19.

## Task and acceptance contract

`acceptance_v19.py` keeps a host-only interpretation ledger. An authenticated
worker statement can be an outstanding offer, never a grant. The host resolves a
short subject confirmation against all compatible outstanding offers, independently
of the selected grant ID. Explicit recipient names/IDs and explicit weekday/task
references take precedence. Multiple compatible offers remain ambiguous; neither
speaker order, recency, role/title, active grants, nor the session initiator breaks
a tie. An unresolved condition remains a reference candidate so that it cannot
make a different worker appear uniquely intended.

Each recognized acceptance records the original classification, requesting actor,
information subject, model-selected recipient and grant pointer, source ID/spans,
reason, and supported service. The resolved task links worker, requester,
beneficiary, information subject, original offer, acceptance, and exact source
receipts for limits. The intended service and original supported offer are pinned
across reference repair. These data never come from benchmark annotations or an
entity/role roster.

Authority still requires the current authenticated subject/permission-authority
receipt. Historical offers are worker receipts, not fresh permission statements.
The host may retain an outstanding offer outside the six-turn window to support
delayed acceptance without another semantic call. The normal wire and canonical
authority checks still apply. New worker grants require their own original worker
receipt, current acceptance, subject, supported service, and closed information
fields. Batch 1 supplies identity/name evidence, subject/recipient boundaries,
information-scope checks, and revocation constraints. A grant for another worker,
subject, or accepted task cannot satisfy this acceptance. Independent explicit
permissions are not rewritten into task grants. Existing self-service request and
explicit responsive service-request conventions remain subject to the same
authority checks; this batch does not change task-authority policy.

Offers and tasks recognize explicit cancellation, supported absolute expiry,
worker handoff, and later revocation. Handoff ends the original task but creates
no grant for the successor: the successor needs their own authenticated receipt
and the subject's acceptance. An explicit weekday can distinguish interleaved
tasks for one worker. Ambiguous cancellation retains task-scoped uncertainty and
blocks affected task access. A new acceptance cannot recover an offer predating
an applicable withdrawal. Unrelated scope or export-only withdrawals do not remove
a scheduling task's read authority.

## Repair-state contract

The additive private table is `privacy.permission_intents_v19`, keyed by
`(compartment, job_id, segment)`, with versioned JSON (`contract_version: 1`).
`PermissionUpdater.acceptance_state()` exposes it only to the host policy session.

Recognition commits before operation/reference expansion can reject a grant.
Independent valid fact classifications from that SAME response are decoded and
stored separately from its permission operations. Failed permission repair does
not discard those candidates or manufacture a grant for them. If a processor never
returns a usable structured response, the host does not invent its interpretation.

Once an acceptance exists, a control-repair response must contain `intent_repair`:

- `REFERENCE_REPAIR`: correct recipient/assignment attributes while preserving
  recognized acceptance, subject, intended service, and original evidence.
- `PENDING_RECIPIENT_RESOLUTION`: explicitly retain uncertainty, original intent,
  candidates, and history; publish no task access.
- `SEMANTIC_REASSESSMENT`: explicitly reject a mistaken interpretation, retaining
  the original, contrary authenticated evidence, replacement interpretation,
  and reason. Evidence must be an exact quote in the original source segment,
  authored by its requester, and support the recognized negative/disclaimer
  grammar. A validation error, unrelated earlier refusal, or informational
  paraphrase of “I can make that” is not contrary evidence. Reassessment cannot
  issue compensating grants for the rejected interaction.

The original interpretation is immutable; reference resolution and repair history
are separate. `SERVICE_ACCEPTANCE` cannot silently become `none`/informational.
Independent adverse intents remain required. Read-only binding/extraction repairs
cannot modify controls already validated. Canonical validation repeats the task
and recipient invariant at final publication, including original offer/current
acceptance evidence and current terminal conditions.

Persistent states include `REFERENCE_REPAIR_REQUIRED`, recipient/task/subject/
authority/semantic/condition/reference pending states, `SEMANTICALLY_REASSESSED`,
`ACCEPTED`, `CANCELLED`, `EXPIRED`, `REVOKED`, `HANDED_OFF`, and
`PENDING_CANCELLATION_RESOLUTION`. Pending states grant nothing. Existing exhausted
job/quarantine and protective holds remain; service repair does not clear them.

Acceptance and its new assignment links commit in the existing atomic SQLite
publication transaction. New worker assignments carry `accepted_task_v19`.
Runtime reads and final buffered release require that task to remain accepted and
unexpired, in addition to all existing ACL, Batch 1 revocation, source-liveness,
and binding checks. Publication rollback leaves no accepted task or partial grant;
restart can resume a validated publication without another semantic response.

The existing durable two-response source budget is unchanged. Recognition,
reference resolution, state persistence, and outcome validation are local work.
No task recovery launches another model call. Existing attempt reservations/counts
survive restart; the adapter still defers maintenance after two foreground calls.
Missing bindings remain unavailable rather than purchasing a third response.

## Validation and evidence

Focused regressions are in `security_tests/test_acceptance_repair_v19.py`.
The final targeted run passed **97 tests**: 47 new acceptance/repair regressions,
40 Batch 1 regressions, and 10 retained ownership cases. The known baseline
failure was explicitly deselected in that targeted run.
They cover bad-reference preservation, explicit recipient precedence, ambiguity,
delayed/interleaved replies, same-worker different-task grants, subject/authority
boundaries, reassessment and contrary-evidence failures, independent facts/adverse
controls, restart and call budgets, cancellation/expiry/handoff/revocation,
canonical bypasses, residual scopes/actions, and atomic publication recovery.
Final results and implementation digest are recorded in
`analysis/v19-acceptance-repair/validation.json`.

The broader offline suite includes these tests, all Batch 1 regressions, retained
V19 ownership tests, incremental/V17/V18 permissions, governance, privacy kernel,
two-store and inference tests, and the frozen medical evaluation tests. Its known
result was **405 passed, 1 baseline failure, 33 subtests passed**. That run included
43 acceptance regressions; the final targeted run additionally covers recognition
surviving an independent fact-expansion failure, retained self-service requests,
explicit responsive service requests, and explicit service precedence over a short
confirmation default. The known
baseline failure remains
`test_generic_integration_can_explicitly_leave_ownership_disabled`: the historical
V19 prompt unconditionally includes the OWNS instruction. That unrelated policy
change remains outside this batch.

`analysis/v19-acceptance-repair/replay_003_02.py` reads historical databases with
`mode=ro&immutable=1` and uses a separate in-memory interpretation ledger. Its report
`003-02-offline.json` resolves Jonah's t044 confirmation to **scheduler_tess** using
the t043 offer, identifies the mistaken saved grant's recipient as
**clinician_dr_lin**, preserves `SERVICE_ACCEPTANCE`, and rejects the saved silent
informational repair. It makes no processor call or ACL publication. Retained
end-of-run rows may have been scrubbed by later erasure; this is not a rerun or a
claim about changed historical scores.

## Migration requirements for later batches

1. The table is created idempotently when a V19 updater initializes. Existing
   completed Batch 1 jobs are not replayed or given inferred task rows. Preserve
   both SQLite files and the new ledger when copying/restoring a workspace store.
2. The new prompt ID is
   `permission-inference-v19-intent-preserving-service-v1`. An old-prompt pending
   job is deliberately not silently reinterpreted. It needs a separately reviewed
   migration preserving original inputs/proposals, interpretation evidence,
   history, and already-spent semantic attempts. Changing versions or deleting
   attempts to reset a budget is not a migration strategy.
3. Preserve stable task IDs, original interpretations, JSON contract versions,
   pending/terminal states, `accepted_task_v19`, canonical guards, runtime gates,
   and authorization snapshot participation. Pending rows must never become
   accepted solely because the process restarted.
4. Older readers do not enforce the new task links. Do not downgrade a populated
   Batch 2 store to Batch 1 without an explicitly reviewed conversion of effective
   task restrictions into controls understood by that reader.

Resolution is intentionally a closed English grammar for scheduling, pickup,
billing, and repair offers and clear confirmations. Relative/unsupported conditions,
unknown names, generalized anaphora, or multiple unresolved tasks stay pending.
Semantic reassessment currently supports source-local negative/disclaimer evidence;
broader challenges need an expanded evidence contract. Source context exposes at
most 24 offer summaries while host uniqueness checks consider the full live prefix.
This batch does not resolve legacy protective holds or automatically recover an
exhausted older acceptance after a clarification; a new authenticated instruction
must follow the existing control and budget rules.
