# Per-turn fixes, version 10 — 29 October 2026

All four requested changes are implemented. Initial requester permissions remain
empty; source authentication, incremental context, exact information views,
separate memory/privacy stores and atomic permission publication are retained.

## Changes

1. `permissions.py` stages controls before checking bindings against final
   assignment/restriction state. All new restrictions require explicit model
   applicability decisions. Missing decisions produce paths and required IDs.
   Operation order cannot bypass the check or bind to a revoked assignment.
2. Permission repair counts come from persisted attempts. Three invalid proposals
   stop automatic retries, including after restart. `recovery.json` reports
   `blocked`; unfinished permission jobs still prevent requester access. Invalid
   model output has bounded contract retries, permanent HTTP errors stop, and
   temporary transport failures remain resumable without using semantic repairs.
3. `permission_wire.py` removes duplicate binding bookkeeping and repeated
   unchanged-recipient explanations from model output. Original authorization,
   necessity, scope, context/metadata checks, exact text and restriction decisions
   remain explicit. The canonical ledger and enforcement representation stay
   unchanged. Existing assignments are reused for new information.
4. `evolution_queue.py` records optional work in the existing memory database.
   Searchable fields/access edges still publish each turn. The runner answers
   and scores first, then processes at most one four-field batch per boundary;
   remaining partial batches flush at invocation end. This is deferred batch
   execution, not a background thread. Queue/job IDs survive restart. Revoked,
   deleted or superseded inputs are discarded; core authorization/lifecycle
   checks remain mandatory. Temporary enrichment outages leave visible queued
   work without blocking future source permission updates.

## Checks

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
git diff --check
```

**202 security tests and 53 evaluator tests passed.** New coverage includes all
six grant/restriction/binding orders, precise repair feedback, atomic publication,
final revocation state, durable rejection budgets, compact response evidence and
recipient coverage, separate transient/permanent errors, four-field batching,
optional outages, lost-response/reopen idempotency, inactive grants, pending
permissions and deletion during enrichment model execution.

The preserved offline smoke starts two boundaries, then resumes one more:

```bash
python3 -m evaluation.gatemem_medical --offline --max-boundaries 2 \
  --run-dir analysis/per-turn-fixes-v10-20261029/offline-smoke
python3 -m evaluation.gatemem_medical --offline --max-boundaries 1 --resume \
  --run-dir analysis/per-turn-fixes-v10-20261029/offline-smoke
```

Its final state has **83 completed permission jobs, three completed boundaries,
and four saved/scored original predictions**, under permission version 10 and
the new enrichment-schedule manifest. This smoke intentionally uses the empty
permission test double, so it creates no grants or enrichment fields. Dedicated
queue tests exercise real field publication, batching and restart separately.
Logs are saved as `offline-start.log` and `offline-resume.log`.

## Exact saved t005 failure

```bash
python3 analysis/per-turn-fixes-v10-20261029/replay_saved_failure.py
```

The diagnostic opens both original v9 databases read-only and enables SQLite
`query_only`. It calls validation without publishing or modifying any original
run. The unassessed binding is rejected in **all six operation orders**. Adding
an explicit non-applicability decision, justified by the restriction naming a
different recipient from the binding, passes in **all six orders**. The runtime
does not automatically supply that semantic decision. See `replay-report.json`.

For the same saved proposal and identical source aliases, serialized canonical
response size is **2,900 bytes**, versus **2,215 bytes** in the compact contract:
**23.6% smaller**. This measures response JSON bytes, not tokens or latency.

## Running the new version

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 10 \
  --run-dir runs/gatemem-medical-raw-stream-v10
```

Use a fresh directory: the prompt, response contract, validation and enrichment
schedule changed. Previous v7/v8/v9 experiments, databases and predictions have
not been altered. Existing run directories cannot be silently resumed under the
new prompt/schedule.

The local model health endpoint at port 8000 returned connection refused during
verification. No live-model speed or semantic-quality benchmark was run, and no
full experiment/model server was started. The deterministic checks and saved
failure replay do not establish an end-to-end latency or model-quality gain.
