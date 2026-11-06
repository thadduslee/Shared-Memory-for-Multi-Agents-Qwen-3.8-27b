# V21: AIM-inspired memory on V19 scheduling

V21 is selected explicitly with `PermissionUpdater(..., version='21')` or
`--permission-version 21`. V19 remains available and remains the generic default.
Use a fresh store/run. Original V19/V20 runs, manifests and predictions are not
rewritten or migrated.

## Scheduling and the V20 result

V21 has **no aggregate source or answer deadline**, no 30-second cutoff, and no
timeout-based path that quarantines an event and advances to the next turn.
It retains V19's two semantic responses per source including foreground
maintenance. Normal provider request timeouts and the durable retry controller
still apply. A provider failure is retried/reported as a failure on the same
source; elapsed time alone is never a permission decision.

The inspected V20 restart recorded 35/57 source timings >=29 seconds and a median
foreground time around 29.77 seconds. It used different serving conditions from
the OpenRouter V19 reference; the timings do not establish an architecture-only
causal comparison. Removing a deadline cannot itself guarantee fast model service.

V21 achieves bounded work through indexed compact verdicts, stable semantic
fingerprints, no unknown-subject reader cross product, no redundant self-grant
reviews, bounded batches, cheap local ranking and no default reranker call.
Summaries report actual source/answer latency, p95/max and turns over 30 seconds.
Rarely exceeding 30 seconds is a measurement target, not an implemented cutoff
or a claim established by scripted tests.

## Implemented AIM recommendations

| Recommendation | Implementation |
|---|---|
| Portable shared service | `service_v21.MemoryService`, opaque host-issued bearer sessions, framework-neutral JSON clients, in-process and WSGI adapters; client JSON cannot supply principal/agent/connector identities |
| Personal ownership separate from delegated sharing | V19 trusted OWNS retained; independently validated exhaustive subject attribution, per-subject receipts, original worker/service evidence and all live restrictions remain mandatory |
| Privacy/provenance-scoped bounded deduplication | Exact same-boundary suppression plus an explicit single-batch `ScopedDeduplicator.review`; logical equivalence records preserve immutable source records and invalidate on input/policy/lifecycle changes |
| Actual-state failure diagnosis | Protected `input_manifest.diagnosis_v21`, capped recent update audit, exact hold/job IDs, unknown attribution, pending pairs and answer-part statuses; offline `diagnose_run.py` reads original checkpoint manifests |
| Independent logical stages and compound operations | Named control/fact/verdict slots; valid sections retained when another section fails; legal per-operation identity references; a repair cannot silently discard a recognized control intent after a required-effect failure |
| Cheap staged retrieval | Authorize first; configured lexical/tag/recency scoring; cached disposable tag tokenization; facet and continuing-plan reservations; full evidence packing; optional one-batch reranking disabled by default |
| Modularity and ablations | Closed validated `RetrievalConfig`, pinned in the run manifest; tag weight zero is a no-extra-call ablation; explicit optional reranker; matched-prefix comparison script |

The shared semantic validators used by V20 and V21 are in
`workflow_features.py`. V20's time scheduler remains selected ONLY for version
20. V21 uses V19's update/answer flow with those independently reusable semantic
safeguards. Reusing validators does not enable the V20 deadline state machine.

## Privacy and lifecycle boundaries

- A role, speaker name, creator ID, general-looking fact or deduplication verdict
  cannot grant read access. Speaker, subject, authority, user and agent stay distinct.
- Whole mixed-person facts require current positive access for every disclosed
  subject. Unknown attribution stays protected.
- Scoped deduplication compares only already processable facts with the same
  namespace, subject set, policy/scope, source connector/speaker and evidence kind.
  A shorter recap does not replace a fuller instruction. Exact dedup retains doses
  and negations; optional semantic comparison receives complete bounded facts.
- Deduplication performs no physical merge, erasure, supersession or authority
  mutation. It creates an auditable logical consolidation registry. Original
  facts/receipts remain available for lifecycle checks and diagnosis.
- Input permission checks run before and after model processing and again at
  release, including inputs omitted from the final prose. Revocation blocks stale
  outputs. Source deletion may also require authority reassessment under the
  existing kernel; a comparison cannot bypass it or resurrect the deleted input.
- Erasure is exact and source-grounded, with original timing and condition checks.
  Similarity is never sufficient to delete or revise a fact.
- No retrieval threshold requires a tag match. Recency does not establish
  supersession. Default retrieval introduces no extra model call.
- The service exposes user output only. Scope labels, queue counts, diagnoses and
  policy evidence are operator-only and require the host audit interface.
- WSGI hosting/TLS and authenticating the host's users are deployment concerns.
  `register_authenticated_client` is a trusted host API and must never be exposed
  as an unauthenticated tool or route.

## Recovery and the four V19 cases

Nadia's ordinary-question ID failure is addressed through legal typed references,
independent section retention and original-event reassessment. Tess's wrong-worker
grant reference cannot silently become a completed no-change interpretation.
Owens's generated task label is interpreted against original accepted-service
receipts, with explicit closed exclusions retained. Elena's conditional deletion
uses the existing validated exact-target/timing lifecycle, and a failed event can
be reassessed without inventing a new user statement.

These are mechanisms and scripted regression coverage, not measured recovery of
all native V19 model failures. Exhausted or stale ambiguous controls remain held.
The recovery budget is durable across restarts; it cannot overwrite a later
revocation/erasure. The host can explicitly call
`updater.service_pending(max_batches=1)` without a wall-clock deadline.
The evaluator uses an available V19 source-call slot for one recovery batch; it
does not add a third foreground call. Ordinary answers never create permissions
or replay historical controls.

## Usage and validation

Local regression command (scripted completions, no model/network calls):

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m pytest \
  security_tests evaluation/gatemem_medical/tests -q
```

Functional native smoke (fake processors, not a model-quality benchmark):

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m evaluation.gatemem_medical \
  --permission-version 21 --offline --no-evolution \
  --run-dir /tmp/NEW-v21-smoke --max-checkpoints 2
```

An actual model run is separate work; none was started by this implementation.
The existing live-call opt-in is retained for V21. When running an authorized
evaluation, choose a fresh directory and the same models/provider settings as
V19 for a meaningful matched comparison:

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m evaluation.gatemem_medical.openrouter \
  --permission-version 21 --allow-paid-api \
  --run-dir /absolute/fresh/v21-run --max-checkpoints 147
```

Direct runner ablations are `--v21-tag-weight 0`,
`--v21-context-tokens N`, and `--v21-rerank-candidates N`. The reranker defaults to
zero; nonzero enables one extra batched precision call, subject to the same live
processing/release rules. Reranking cannot add evidence.

Read-only diagnostics and matched comparison:

```bash
python3 analysis/aim-v21/diagnose_run.py /absolute/run --output /tmp/NEW-diagnosis.json
python3 analysis/aim-v21/compare.py --baseline /absolute/v19-run --candidate /absolute/v21-run
```

Diagnosis does not change official scores or award success for a missing answer.
Compartment-wide queue/hold observations are not assertions of exact query
causation. Comparison checks checkpoint/source coverage, model/data identity,
new leaking cases, deletion leakage, previously passing utility and latency.
Partial matched-prefix success cannot establish full-dataset success.

The portable example is `examples/portable_memory_v21.py`; it requires an
explicitly chosen model endpoint and does not start a server. Tests exercise
two agent identities using in-process and JSON/WSGI adapters, unauthorized users,
identity forgery, worker revocation, stale release, deduplication, late completion,
restart budgets, independent sections and mixed-subject facts.

Completed local validation: **692 regressions passed**, including **38 V21
checks**. The fresh offline native smoke completed 67 source turns and two
checkpoints, with a code digest matching the implemented runtime. Scripted
completions establish integration and enforcement behavior, not real-model
utility, leakage rates or latency. No live benchmark was started.

Exact commands, report hashes and coverage are in [validation.json](validation.json).
The full regression report is [regression.xml](regression.xml), and V21's
focused report is [v21-tests.xml](v21-tests.xml).

A subsequent [citation transport repair](answer-reference-repair-20261102/README.md)
corrects validation order for compact answer/evolution references. Its expanded
suite passes 712 tests; the native GPU run continues through an explicitly
recorded code revision with the original source snapshot and failures preserved.
