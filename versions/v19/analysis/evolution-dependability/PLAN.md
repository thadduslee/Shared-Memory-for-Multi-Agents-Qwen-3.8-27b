# Evolution dependability evaluation — 25 October 2026

The requested scope is the first two medical episodes, sorted by episode ID:
431 source turns, 52 boundaries and 56 original queries per arm. This includes
the interrupted run's boundary 28. Original run artifacts remain unchanged.

Compare fresh evolution-off and evolution-on stores with the same source-prefix
schedule, embedding fingerprint, local answer model, local judge, prompts and
scoring implementation. No annotation or judge feedback is an ingestion input.
The available local Qwen3-14B judge is used for both arms; these results should
not be directly equated with the earlier GPT-4.1-judged run.

Primary outcome: paired utility correctness wins and losses. Report the raw
counts and exact paired test, including the appointment-query subset. Report
action correctness separately from semantic answer correctness.

Privacy outcomes: judged privacy/deletion answer leakage and auxiliary context
exposure, including newly leaking checkpoints even if aggregate rates tie.
A utility increase accompanied by new leakage does not meet the intended goal.
This small sample cannot prove absence of privacy leakage.

Dependability outcomes: completion of all selected boundaries, completion past
boundary 28, model proposal rejections, repaired jobs, exhausted jobs, no-op jobs,
and atomicity/privacy tests. Failed optional evolution must preserve source memory
and remain visible in the result, rather than becoming an unreported success.

Inspect completed stores for integrity, dangling bindings, deleted vectors,
diagnostic indexing, full-input dependencies, unsupported context excerpts, and text differing from
its canonical source. Record total boundary time and evolution attempt time
(including model generation, validation and publication).
Single temperature-zero generations still permit model/judge variability; any
identical-output verdict disagreement must be disclosed.

An initial partial trial rejected harmless descriptive tags by literal matching
and was preserved as `runs/evolution-ablation-20261025-v1/`. It is not the final
comparison. The revised contract permits descriptive retrieval tags and includes
the rejected proposal in correction feedback; generated context remains a source
excerpt and generated metadata remains outside answer evidence.

A second partial trial is preserved as `runs/evolution-ablation-20261025-v2/`.
Review found that canonicalizing an overlay-selected neighbor could discard its
narrower audience. The final implementation retains that overlay as a dependency
while supplying only canonical source text to the model. A regression test checks
the private selection lineage through another evolution and deletion.

The final two arms run concurrently against the same local model services, in
separate processes/stores. Concurrency can also affect model inference variability.

Final artifacts: `runs/evolution-ablation-20261025-v3/{baseline,evolved}/`,
`comparison.json`, `paired_queries.jsonl`, and a separate validation/report here.
