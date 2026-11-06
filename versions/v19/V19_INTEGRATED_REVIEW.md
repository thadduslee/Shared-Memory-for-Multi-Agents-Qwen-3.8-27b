# Integrated V19 batches and fresh evaluation

Workspace `/home/thaddus/a-mem-v19-fixes`, branch `v19-recipient-revocation`.
The original checkout, historical runs and uncommitted V20/V21 work remain
untouched. Frozen V19 baseline: `3d0bc0c`; archive/hash proof: `V19_BASELINE.json`.

| Batch | Actual audit trail and behavior | Remaining limits |
| --- | --- | --- |
| 1 | `ee4cfc1`: grounded recipient identity, complete recipient-specific revocation, residual scopes/actions. `V19_BATCH1.md`. | Unsupported/ambiguous references stay protected. |
| 2 | `2021244`: immutable accepted-service interpretation, original offer/acceptance evidence, recipient repair and task lifecycle. `V19_BATCH2.md`. | Closed grammar, unresolved conditions and older exhausted jobs do not auto-recover. |
| 3 | **No separately implemented Batch 3 commit/document exists here.** Batch 4 explicitly assumes none. Personal OWNS behavior is retained from the verified baseline. | Do not invent completion or import another version's workflow. |
| 4 | `30dc075`: scoped control holds, durable uncertainty, preservation of proven unaffected bindings. `V19_BATCH4.md`. | Unknown boundaries remain conservative; an approval cannot clear a hold. |
| 5 | `e4a095f`: disclosed-subject attribution, correction/quarantine, safe projection and upstream subject filtering. `V19_BATCH5.md`. | Mixed/unknown subjects and unsafe projections stay unavailable; old extraction gaps are not reconstructed. |
| 6 | `4f11fe4` / `5672313`: structured task contracts, trusted dependencies and authenticated host-event JIT. This answer-validation follow-up adds enforced request/claim certificates and bounded repair. `V19_BATCH6.md`, `V19_ANSWER_VALIDATION.md`. | Maps are not authority; raw benchmark supplies no trusted host events. Semantic quality/latency need real-model measurement. |

This covers all six numbered slots without claiming a nonexistent Batch 3
implementation. The current answer work preserves the preceding Batch 6 receipt
work rather than replacing its audit or implementation.

All earlier release checks remain necessary: live source/attribution, current
recipient-specific grant/task/lifetime, individual binding approval, absence of
applicable restrictions/revocations/erasure/holds, and final reauthorization. The
answer now additionally needs relevant, entailed, current and adequately covered
evidence. Valid IDs, exact quotation, roles or dependency maps alone are insufficient.

## Unresolved issues

- Real-model source interpretation/extraction and bounded reviews can still omit
  facts, over-narrow accepted work or exhaust control repair. Answer validation
  cannot repair absent authority or retrieve hidden data to satisfy a checkpoint.
- Baseline test `test_generic_integration_can_explicitly_leave_ownership_disabled`
  remains an ownership-prompt expectation failure documented in earlier batches.
  This answer change does not alter that unrelated policy/prompt.
- Registry/reference/acceptance grammars are conservative. Unknown tasks, mixed
  records, missing authorized subject context and unsupported conditions can remain
  unavailable. Historical exhausted jobs are not silently replayed.
- The raw benchmark supplies no trusted assignment stream. A worker recap/title
  is insufficient. Keep dialogue acceptance and externally authenticated host-event
  experiments separate in configuration, provenance, coverage and scores. Do not
  manufacture events from roles, entities, relationships, gold or future turns.
- Semantic verification can falsely accept/reject or miss coverage/qualifiers.
  Measure actual-model paraphrase/normalization acceptance, category rejection,
  current-state coverage, injection resistance and adjudicated false rejection.
  It adds a model call; historical-V19-equivalent latency is not established.

## Fresh evaluation without overwriting history

1. Record the isolated workspace revision and local test/replay results. Freeze
   code during a run so implementation-digest/resume checks remain meaningful.
2. Allocate a **new empty** run directory. Never reuse historical directories,
   journals, predictions, source archives or memory/privacy stores. Do not seed
   a fresh experiment by copying populated stores or resetting attempt budgets.
3. Pin dataset/input allowlist, embedding fingerprint, model/backend/provider
   routing, caps/timeouts, schedule, query limits and evolution settings. Record
   the new code digest and `answer_validation.mode=enforce`. Shadow is a separately
   labeled ablation, not the active fix. Keep raw-stream registry/gateway disabled;
   optional real upstream host events require a separate configured cohort.
4. When separately authorized, run a bounded real-model pilot and independently
   adjudicate relevance/entailment, partial/denied/erased handling, continuing
   requirements, false acceptance/rejection, and latency distributions. Separate
   source calls from query generation, policy classification and verification.
   Measure matched end-to-end and verifier p50/p95 under the same endpoint/load;
   scripted timing is not a proxy.
5. For an authorized fresh run, the following is a **procedure only**, not a
   command executed for this task:

   ```bash
   cd /home/thaddus/a-mem-v19-fixes
   new_run=$(mktemp -d /home/thaddus/v19-enforced-eval-XXXXXXXX)
   /home/thaddus/gatemem/.venv-nb/bin/python -m evaluation.gatemem_medical \
     --permission-mode inference --permission-version 19 \
     --answer-validation-mode enforce --env-file /path/to/pinned-model.env \
     --run-dir "$new_run"
   ```

   Endpoint configuration must already be pinned/authorized; no secret belongs
   on the command line. Omit `--resume` for a fresh run. Resume only that same
   experiment with matching code/configuration. An acknowledged code revision is
   a mixed run, not a fresh matched evaluation. Ensure free disk space first.
6. Export coverage, immutable predictions/scores and per-query validation receipts
   to the **new** directory. Report source-call distributions, verifier costs,
   failures/repairs, adjudicated false rejections, unresolved holds, attribution/
   binding backlogs and missing host events. Score with evaluator-only annotations
   after inference; never feed them back into validation or authority provisioning.

No paid pilot or full paid benchmark was launched for this implementation task.
