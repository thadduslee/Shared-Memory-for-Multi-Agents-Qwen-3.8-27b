# Sliding permission state — 29 October 2026

The default updater and GateMem runner now use permission version 11 and prompt
`permission-inference-v11-sliding-window`. The model receives at most six source
events (including the new one), complete concise current policy and trusted
identity/authority configuration. Historical evidence, explanations, binding
samples and retrieval indexes are absent. There is no history retrieval path.

## Checks

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
git diff --check
```

238 security tests and 53 evaluator tests passed. The security suite includes
23 new sliding-window cases. They exercise strict event boundaries even after
deletion, evidence-free policy persistence, retained host audit and bindings,
new facts under old committed grants, unauthorized scope changes, old-source and
history-request rejection, cross-recipient revocation, event expiry, pending task
acceptance outside the window, retained expiry/information needs, source deletion,
new restrictions over historical grants, compact schema/repair behavior, atomic
rollback, restart and rejection of unsafe legacy-store conversion. Deleting the
entire window allows only an evidence-free no-op review so host invalidation can
complete; it cannot authorize new operations or backfill older sources.

Legacy fixtures explicitly select their older version to retain tests for v10's
context retrieval, original-evidence validation and bounded binding samples.
The evaluator's default-path tests exercise v11, including source boundaries,
grant compilation, deferred enrichment and resume. No live model was queried.

## Request-size comparison

```bash
python3 analysis/sliding-permissions-v11-20261029/measure_context.py
```

The script uses temporary stores and the deterministic permission fixture. It
establishes a narrow ride grant, binds twenty more pickup facts, ingests eighty
unrelated observations, and binds a new pickup time. It reconstructs v10 and v11
requests from the **same pre-update policy snapshot**, enabling compact responses
and recipient coverage for both. Existing run artifacts are never opened or
modified. The temporary stores are deleted after the measurement.

Counts are serialized UTF-8 **instruction + payload + response schema** bytes,
not tokenizer output, exact HTTP envelopes, measured latency or inference quality.

| Measurement | v10 reconstruction | v11 |
| --- | ---: | ---: |
| Source turns sent | 11 | 6 |
| Permission-state bytes | 1,319 | 488 |
| Source payload bytes | 3,752 | 1,633 |
| Instruction bytes | 18,054 | 4,413 |
| Response-schema bytes | 42,121 | 21,699 |
| Total serialized request bytes | 69,953 | 30,779 |

The request is **56.0% smaller** in this synthetic case. All 22 information
bindings remain in host storage, the new pickup fact remains readable, and
current policy remains 488 serialized bytes. The source input remains six turns
as unrelated history grows. State size still depends on the actual number of
grants, activities, restrictions and pending decisions.

See [measurement.json](measurement.json) and the readable
[current ACL sample](access-state.sample.json). This is generated v11 policy,
not a migration of an existing v10 ledger.

## Behavior and limits

The current-policy table in a new v11 `memory.privacy.sqlite` is
`permission_access_state`. `PermissionUpdater.access_state()` returns the same
policy projection. `permission_records`, `permission_attempts` and
`permission_dependencies` retain separate host-only audit and invalidation data.
They do not enter the model's policy snapshot. Current policy and audit publish
atomically; unchanged current-policy bodies are not rewritten.

New bindings reference accepted assignments without model-supplied original
authorization evidence. The host resolves and validates that lineage itself.
New control changes still require authenticated evidence within the fixed window.
Ambiguity that cannot be resolved from the window and current policy stays
unresolved. New restrictions review affected committed scopes; overlapping grants
must be suspended/revoked, with a fresh narrow replacement if supported. This can
conservatively reduce old access instead of reclassifying unseen historical text.

Host validation still loads the live source prefix and exact binding history.
This change reduces model input and schema duplication, not all host CPU work.
There is no live-model latency or quality result for v11 yet. Existing v10 runs
were not changed or relaunched. Start a fresh v11 run directory and compartment;
legacy labels may depend on limits present only in their old evidence.
