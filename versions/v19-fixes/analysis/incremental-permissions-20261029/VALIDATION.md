# Incremental permission context — 29 October 2026

The default updater now selects permission interpretation context instead of
replaying all source text and binding history. Full host validation and atomic
publication remain in place. See [the contract](../../PERMISSION_INFERENCE.md#incremental-context-version-9).

## Deterministic verification

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
```

192 security tests and 42 evaluator tests passed. Fourteen incremental scenarios
cover bounded history, retained scope/expiry, short acceptance and pending work,
cross-recipient revocation, bounded binding samples/pages, omitted-record
discovery, mandatory retrieval of unloaded evidence/records, atomic search then
publication, source isolation, alias handling, processor failure/resume, persistent
retrieval budgets and snapshot conflicts. Existing deletion, restriction,
erasure, authority, replay and rollback tests also pass.

## Synthetic request-size measurement

```bash
python3 analysis/incremental-permissions-20261029/measure_context.py
```

The script creates a temporary store using the security-test fixture, establishes
a narrow ride permission, adds 80 unrelated observations and binds a new pickup
time. It reconstructs the prior full-prefix compact input and response schema
from the same pre-update canonical ledger. Both use the same scope, sources and
model contract; the baseline omits the new incremental instructions and retrieval
schema. Counts measure UTF-8 serialized **instruction + payload + schema**, not
tokenizer output or exact HTTP-envelope bytes.

| Measurement | Full-prefix reconstruction | Incremental |
| --- | ---: | ---: |
| Source turns sent | 83 | 9 |
| Serialized bytes | 130,895 | 49,765 |
| Calls for the last update | 1 | 1 |

The incremental request is 62.0% smaller. The original grant remains active and
both the old and new authorized pickup times remain readable. The baseline call
count is the prior single-call architecture, not a separately executed model.
All execution uses the scripted processor; no live model was queried.

This is a synthetic size check, not a GateMem result, semantic-accuracy test,
latency measurement or guarantee of constant context size. Relevant control
evidence, pending work, the ledger index and global restrictions can still grow.
Host-side source loading/validation still examines the complete live prefix.
Lexical retrieval and the model's decision to request missing context require
real-model evaluation in a fresh version 9 run.
