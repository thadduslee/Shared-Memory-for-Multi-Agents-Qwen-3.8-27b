# Evolution dependability and paired evaluation — 25 October 2026

Raw judged utility decreased in this selected sample. No new answer leakage or aggregate context-exposure increase was observed. The sole raw utility loss reflects inconsistent judging of nearly equivalent answers; it does not establish a causal quality regression.

Both arms completed 431 source turns, 52 boundaries and 56 queries in separate stores. There are 20 utility, 18 privacy and 18 deletion queries per arm. Both used the same source prefixes, code, embedding and local model services. The answer/evolution model was Qwen3.8-27B and the judge was Qwen3-14B. These are not fresh GPT-4.1 judgments and should not be compared directly with the interrupted run.

| Measure | Evolution off | Evolution on |
| --- | ---: | ---: |
| Utility correct | 17/20 | 16/20 |
| Privacy answer leakage | 0.0% | 0.0% |
| Deletion answer leakage | 0.0% | 0.0% |
| Privacy context exposure (auxiliary) | 0.0% | 0.0% |
| Deletion context exposure (auxiliary) | 0.0% | 0.0% |
| Boundary time (shared servers) | 9.4 min | 81.4 min |

Paired utility: 0 wins, 1 loss(es), 19 ties. Exact paired two-sided p = 1. A single small two-episode experiment cannot establish general improvement or prove privacy.

The appointment-query utility subset tied: 4/6 in both arms.

Boundary 28 completed: **True**. Job states: `{"applied": 131}`; 131 attempts, 0 rejected proposals, 0 jobs accepted after correction, 8 empty jobs. 0 repaired jobs produced non-empty outputs.

The implementation permits at most three proposals by default. Only model contract errors receive correction attempts; permission changes and processor failures remain errors. An exhausted optional evolution job preserves canonical notes, records its rejection and lets the boundary continue. Raw proposals and validation paths are unindexed resources restricted by every consumed input and cleared by dependency deletion.

Context must quote its target source note. Descriptive tags influence retrieval only. The answer model receives canonical source text; generated descriptions never become factual answer evidence. An overlay used to select a neighbor remains a dependency even when only the canonical source enters the prompt.

Evolution was consumed by 11 queries through enrichments and 14 through edges. See paired_queries.jsonl for changed answers and the appointment-query subset in comparison.json.

Store validation:

```json
{
  "baseline": {
    "memory_integrity": "ok",
    "privacy_integrity": "ok",
    "active_enrichments": 0,
    "active_diagnostics": 0,
    "grounding_or_diagnostic_violations": [],
    "lineage_errors": [],
    "indexed_diagnostics": 0,
    "deleted_vectors": 0,
    "dangling_access_bindings": 0,
    "active_resources_without_primary_binding": 0,
    "nonempty_applied_jobs": 0,
    "nonempty_repaired_jobs": 0,
    "evolution_attempt_seconds": 0,
    "total_boundary_seconds": 565.7237349590287
  },
  "evolved": {
    "memory_integrity": "ok",
    "privacy_integrity": "ok",
    "active_enrichments": 398,
    "active_diagnostics": 110,
    "grounding_or_diagnostic_violations": [],
    "lineage_errors": [],
    "indexed_diagnostics": 0,
    "deleted_vectors": 0,
    "dangling_access_bindings": 0,
    "active_resources_without_primary_binding": 0,
    "nonempty_applied_jobs": 123,
    "nonempty_repaired_jobs": 0,
    "evolution_attempt_seconds": 4168.658883868717,
    "total_boundary_seconds": 4884.799648858607
  },
  "identical_output_verdict_disagreements": [],
  "queries_consuming_evolution": {
    "enrichments": 11,
    "edges": 14
  }
}
```

There are 82 passing security tests and 13 passing scoring/comparison tests. They cover correction feedback, exhausted jobs, atomic publication, source grounding, private overlay selection lineage, deletion, permission changes and protected diagnostics.

The boundary times above were measured while the two arms shared model servers concurrently; they are descriptive run timings, not an isolated throughput benchmark. Temperature zero does not remove all generation or judge variability. Any verdict disagreements on identical outputs are listed in validation.json.

Artifacts: [evolution-ablation-20261025-v3](/home/thaddus/a-mem/runs/evolution-ablation-20261025-v3) and [comparison.json](/home/thaddus/a-mem/runs/evolution-ablation-20261025-v3/comparison.json). The original interrupted run was not changed. Earlier partial trials are excluded.

Code digest: `4aa4fae8111b24f50d3c1034ce4801afef4ebb8a4b4113050bbd7b78adad6aeb`. Dataset digest: `d6a7cbce087851081bc8c4ccbe328329481aed15d256cb9b064e3ef6e5a31078`.

See [CASE_AUDIT.md](/home/thaddus/a-mem/analysis/evolution-dependability/CASE_AUDIT.md) for a raw utility loss in which both answers omit the same required facts but receive different judge verdicts. The raw score delta is not reliable evidence of a causal answer-quality regression.

The measured change improves execution dependability but does not demonstrate better answers. Use `--no-evolution` for subsequent answer-quality runs until a further change shows a utility gain without privacy regression. The current default remains configurable through that flag.
