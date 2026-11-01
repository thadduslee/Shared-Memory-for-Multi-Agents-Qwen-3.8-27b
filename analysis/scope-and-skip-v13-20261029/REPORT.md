# Scope selection and exhausted permission turns

The t087 failure attached emergency warning instructions to the patient's
appointment-only grant for a driving task. Another attempt invented nurse
permission authority. The revised prompt requires matching exact information
scopes and retaining facts without availability bindings when no supported grant
fits. Recipient-completeness errors now include the activity, source, reviewed and
missing assignment IDs, precise scopes, and a repair instruction allowing an
explicit out-of-scope decision or no bindings.

An isolated local-model probe used only the original t087 prefix and stored policy.
It returned the warning instructions as a new fact, permission_changes: [], and
make_available_under: []. No new grant or scope expansion was proposed. The original
experiment databases were opened read-only. See t087-live-probe.json; this single
probe does not establish full-run accuracy.

The v13 evaluation adapter now calls skip_exhausted() after three rejected
permission proposals and continues to the next source. Skipping publishes no
rejected changes and preserves all attempts and the error. It is idempotent across
restart. The core still raises on exhaustion by default; other permanent errors
continue to surface normally.

A skipped permission turn remains a protective runtime access hold for that
compartment, because its text may contain an unprocessed revocation. Later turns
continue to ingest and be assessed. Checkpoints for that episode report missing
information and context_complete: false, with skipped_permission_turns in their
input manifest. The run summary lists skipped jobs and marks permission inference
incomplete/degraded separately from schedule and checkpoint coverage.

Validation: 326 security tests and 58 evaluation tests passed. Regression coverage
includes exact missing-recipient feedback, retaining an unbound fact after repair,
continuation past an exhausted turn, immutable failure history, idempotent restart,
protection against stale access after an unprocessed revocation, and checkpoint
creation after a skipped permission turn.

The existing full experiment was resumed in amem-eval:1 with an explicit code
revision acknowledgement. Four previously completed predictions remain immutable;
implementation_revisions.json records the transition. The already exhausted t087
job is skipped rather than resetting its three-attempt budget.
