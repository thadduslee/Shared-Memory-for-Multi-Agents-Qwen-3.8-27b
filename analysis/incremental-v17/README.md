# Selective incremental memory — v17

`PermissionUpdater` and the raw-stream evaluator now default to version 17. Use
a fresh store; historical inference versions and run directories remain available.
The legacy `AgenticMemorySystem` is still unguarded. Requesters must use the
privacy runtime and an authenticated application adapter.

## Implemented behavior

| Requirement | Implementation | Verification |
| --- | --- | --- |
| Correct scoped access, fewer false refusals | Required source-intent slots identify the actual recipient. A selected service request requires that reader's grant or a live matching assignment. Worker grants name necessary fields and retain original authority receipts; independent field reviews remain mandatory. Questions can request a service without becoming facts. | `test_selective_memory_v17.py`; real-model generic probe |
| Separate controls | Communication preferences are protected instructions, not reading restrictions. Typed intents must agree with actual grant/denial/revocation/erasure operations. Restriction replacements cannot change their subject or recipients. Access-routing status remains a protected fact about the controlled resource. | Contact-preference, recipient, revocation and live routing checks |
| Reuse safe work | Review fingerprints exclude binding history before copying. Context hashes are reused within a batch. Exact grants update edges directly; publication reuses unchanged views and vectors. Unchanged capability targets are not re-approved as new targets. All affected data remains subject to live checks. | Security suite and identical-decision host benchmark |
| Precise active forgetting | Erase exact values and descendants, preserve unrelated review receipts and facts, and block later restatements/confirmation copies. An attribution receipt mentioning a deleted value does not erase another safe field from the same source. | Same-source scoped erasure and resurrection tests |
| Deferred forgetting | Every v17 erasure explicitly selects now, an offset timestamp, or an exact conditional request. Time-based requests fail closed when due and are physically swept by trusted ingestion/timer maintenance. Conditional completion needs fresh authenticated authority evidence. | Early/due/conditional/unauthorized-completion tests |
| Complete permitted answers | V17 answers are assembled from the actual authorized evidence selected as responsive, preserving whole instructions and their qualifiers. Model-generated guesses are not released as factual prose. Mixed requests retain permitted parts; missing information and denial stay distinct. | Portal omission/guessed-prose regression and live probe |
| Honest observability | Scalar timing survives text-cache scrubbing. Reports distinguish current pending reviews from repeated historical observations, record model versus other time and early/late source latency, and mark unfinished bindings as degraded. | Regression suite, host benchmark, native run reports |

The main call reviews at most four own-subject bindings; the independent focused
call handles up to 24 indexed pairs. Source processing still has at most two
semantic responses. Overflow is protected and reported; hosts may explicitly
schedule `flush_bindings()` maintenance. The evaluation adapter schedules at most
one additional maintenance batch after each source event, independent of queries
or checkpoint placement. This is additional model work outside the two-response
update budget; its time is included in source_total and reported separately.
Maintenance uses the same exact-fact format and original authority receipts as
the normal focused review, and every receipt is included in processor input checks.
No requester query grants access or
triggers permission inference. These bounds limit work per response, not arbitrary
document size or end-to-end latency.

## APIs and deployment

```python
updater = PermissionUpdater(core, host_context, namespace, bootstrap, processor)
service = PermissionQueryService(updater)
result = service.answer(authenticated_requester_id, question, processor)
# Host timer; never expose as an untrusted requester tool:
updater.process_scheduled_forgetting()
```

The bootstrap must authenticate sources/callers and explicitly establish resource
authority and the supported task convention. Names, occupations and ownership are
not automatic read grants. Natural-language interpretation remains fallible.

Deferred requests live in `scheduled_erasures`; completed/cancelled requests scrub
their stored values. If a scheduled request's authority evidence or policy contract
is invalidated, it cannot execute. Due hash checks protect reads before the next
physical sweep. Existing tombstone, source-liveness, dependency, expiry, denial and
buffered-release checks continue to apply. Protected original source/audit retention
follows the repository's logical-erasure contract; this is not external provider
data deletion or physical backup destruction.

`AMEM_SOURCE_SCHEMA_MODE=strict` retains the provider's structured-output grammar.
The optional `json` mode sends the same grammar as `output_contract` and validates
it locally with JSON Schema before canonical authorization checks. Invalid output
never publishes; this option does not bypass any permission validation. The selected
mode is pinned in the evaluation manifest. `AMEM_OPENROUTER_PROVIDER_ORDER` optionally
prefers providers for the same model while retaining compatible fallback; provider
preferences are also recorded. See [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

## Reproduction and full evaluation

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m pytest security_tests \
  evaluation/gatemem_medical/tests -o addopts='' -q
PYTHONPATH=. /home/thaddus/gatemem/.venv-nb/bin/python \
  analysis/incremental-v17/benchmark.py --output /tmp/fresh-host-benchmark
PYTHONPATH=. /home/thaddus/gatemem/.venv-nb/bin/python \
  analysis/incremental-v17/probe.py --output /tmp/fresh-live-probe
```

`full_session.py run RUN` freezes the implementation into RUN/frozen-source and
archives it without credentials, then processes all 21 episodes, 4,294 source
turns, 567 boundaries and 579 native checkpoints, including source-only tails.
`full_session.py watch RUN` monitors actual process liveness, source timings,
scores and current readiness. Existing runs are never overwritten, predictions
and scores remain immutable, and scored failures do not stop the full schedule.
The first available matching prefix is compared with the original v16 run using
the same checkpoint order, dataset, embedding, memory/answer and judge models.

Unit tests prove deterministic contracts, not semantic accuracy. Development
probes and host-only timings are not full-dataset quality or latency results.
Final evidence and remaining failures belong in REPORT.md after verification.
