# SimpleMem / EvolveMem review and combined research direction

Reviewed on 2 November 2026 from `/home/thaddus/SimpleMem`, commit `db80b6a7c591e0ea730a058e9f5fc4eb06572299`. This is source inspection, not reproduction of the repository's benchmark claims. No model calls, experiments, package installation, or runtime changes were performed.

This note carries forward [the V19 failure review](/home/thaddus/a-mem-variant/analysis/over-refusal-v19-review-20261102/review.md) and [the AIM reading notes](/home/thaddus/a-mem-variant/analysis/aim-paper-review-20261102.md). The user's continuing constraints are low source/answer latency, preserving low answer/context privacy leakage and zero observed deletion leakage, and reducing over-refusal without weakening authorization.

## Main assessment

The strongest transferable idea is an offline, failure-driven experiment loop that changes a bounded retrieval strategy, compares it against the incumbent, and preserves rollback history. This complements AIM's portable service boundary and your existing privacy kernel.

Keep the distinction between **retrieval strategy** and **authorization policy** explicit. EvolveMem's `MemoryPolicy` primarily controls ranking weights and injection budgets; it is not equivalent to your permission ledger. A successful retrieval experiment must not create a grant, infer a new information subject, clear a control hold, bypass an erasure, or weaken a release check.

The local repository contains several relevant layers:

- SimpleMem text memory: structured compact facts and semantic/lexical/symbolic retrieval.
- `EvolveMem/evolvemem`: benchmark evolution plus a store/manager/replay/promotion path.
- `simplemem/evolver`: packaged counterparts and the `simplemem.optimize()` adapter.
- `cross/`: session lifecycle, context packing, provenance, and HTTP/MCP integration.

The review concentrated on the text and EvolveMem paths; multimodal documentation was read for architectural context, not audited in depth.

## What AutoResearch means in this checkout

[EvolutionEngine.evolve](/home/thaddus/SimpleMem/EvolveMem/evolvemem/evolution.py:402) runs extraction/indexing, QA, scoring, diagnosis, bounded configuration proposals, and further evaluation rounds. It retains attempt history and best-so-far configurations. [RetrievalConfig](/home/thaddus/SimpleMem/EvolveMem/evolvemem/multi_retriever.py:39) exposes view depths, fusion modes/weights, context size, optional decomposition/reflection, verification, diversity, and category-specific choices.

There is a useful limit to the autonomy claim. The [meta-analyzer](/home/thaddus/SimpleMem/EvolveMem/evolvemem/meta_analysis.py:85) describes new mechanism proposals as logged for humans; [the evolution loop](/home/thaddus/SimpleMem/EvolveMem/evolvemem/evolution.py:736) appends them to `meta_proposals.jsonl`. The inspected application path applies supported dataclass/configuration fields. It does not automatically implement arbitrary new retrieval code from those proposals. Capabilities such as decomposition already have implementation and configuration switches in this checkout.

The [packaged optimizer](/home/thaddus/SimpleMem/simplemem/evolver/optimize.py:1) explicitly calls itself a reduced retrieval tuner: it uses the supplied development QA pairs, already-built memory, and `adapter=None`. This simpler interface is closer to the adaptation we need than the full benchmark-specific runner, but it is not a drop-in replacement for your guarded runtime.

## Transferable ideas

### 1. Bounded offline evolution with constrained promotion

**Source mechanisms:** [diagnosis](/home/thaddus/SimpleMem/EvolveMem/evolvemem/diagnosis.py:297), [candidate generation](/home/thaddus/SimpleMem/EvolveMem/evolvemem/candidate.py:8), [replay](/home/thaddus/SimpleMem/EvolveMem/evolvemem/replay.py:130), [promotion](/home/thaddus/SimpleMem/EvolveMem/evolvemem/promotion.py:20), and [revision/rollback history](/home/thaddus/SimpleMem/EvolveMem/evolvemem/policy_store.py:35).

**Adaptation:** Diagnose the earliest failing stage, propose one small ranking/context change, evaluate on isolated causal snapshots, then retain or discard it. Run the expensive diagnosis outside the foreground turn path. Deploy a fixed, versioned strategy until another validated experiment replaces it.

An initial action space could include the existing lexical weight, recency bonus, per-facet evidence allocation, deduplication threshold, and bounded graph expansion depth. Later candidates could compare weighted fusion with reciprocal rank fusion. Use actual ranges only after measuring the current baseline. Do not let the optimizer change authentication, grant creation, subject attribution, control-hold semantics, deletion, or final release behavior.

Your search already combines semantic/lexical relevance, query facets, recency reservations, and duplicate suppression. The new contribution would be controlled selection among these choices, not the first introduction of hybrid retrieval.

**Promotion criteria:** Improve strict utility or reduce genuine over-refusal while adding no newly leaking cases on the matched development/regression set; preserve zero deletion leakage; protect previously passing cases; satisfy source and answer latency/token/call budgets. A passing finite replay is evidence, not a guarantee for unseen requests. A new leak should reject a candidate even when total leakage count stays unchanged by fixing another leak.

### 2. Complementary retrieval views with rank fusion

**Source mechanism:** [multi-view retrieval](/home/thaddus/SimpleMem/EvolveMem/evolvemem/multi_retriever.py:653) combines semantic, BM25, and structured metadata search; [_fuse](/home/thaddus/SimpleMem/EvolveMem/evolvemem/multi_retriever.py:586) supports weighted sums and reciprocal rank fusion.

**Adaptation:** Rank the same already-authorized facts through complementary views. Dense similarity can cover paraphrases; lexical matching can preserve drug names, exact times, and numbers; verified metadata can select subjects, active plans, and event types. Rank fusion is worth testing because scores from different views need not share a scale.

All views must respect the original authenticated requester, exact grants, current lifecycle, approved processors, and subject boundary. A name extracted by a query analyzer is a search hint, not a new identity or permission. Do not retrieve unrestricted top-k and merely remove private hits afterward.

**Expected effect:** Better coverage where allowed facts exist but the selected packet omits them, usually without an additional LLM call. This does not recover a fact held unavailable by the policy engine.

### 3. Coverage-aware allocation for multi-part questions

**Source mechanisms:** [SimpleMem's retrieval planner](/home/thaddus/SimpleMem/simplemem/core/hybrid_retriever.py:81), [EvolveMem query decomposition](/home/thaddus/SimpleMem/EvolveMem/evolvemem/evolution.py:1265), and optional coverage reflection.

**Adaptation:** Allocate the existing context budget across explicitly requested parts and preserve the coherent plan: current items, unchanged requirements, cancellations, conditions, arrival/preparation details. For an allergy-and-schedule question, one highly relevant schedule paragraph should not consume all opportunities to retrieve the allergy fact.

The current search already splits bounded facets; test improving that allocator before adding model-based planning. Keep the original subject and permissions across every subquery. A planner must not infer a diagnosis, invent a drug, or expand a request into another person's records.

Use an additional reflection/verification pass only if it proves valuable within the declared deadline and call budget. A recovery path should know whether it is addressing missing evidence, pending authorization work, or incomplete answer composition.

### 4. Compact derived views with exact source lineage

**Source mechanisms:** [MemoryEntry](/home/thaddus/SimpleMem/simplemem/core/models/memory_entry.py:13) and [MemoryBuilder](/home/thaddus/SimpleMem/simplemem/core/memory_builder.py:20) represent self-contained facts, resolved references, and timestamps.

**Adaptation:** Add compact retrieval views to immutable source-backed facts, rather than replacing the authoritative source with a paraphrase. Preserve doses, negation, exceptions, conditional/future actions, cancellations, subjects, and provenance. A source-span-backed view such as “nifedipine ER 30 mg nightly; labetalol stopped” can be efficient if its full policy and lifecycle dependencies remain intact.

“Lossless restatement” is a design goal and schema name, not proof that an LLM preserved every necessary detail. In your medical stream, density gating must not discard a short revocation, prohibition, allergy, or deletion instruction as unimportant. Semantic similarity must not itself choose an erasure or supersession target.

This connects to AIM's privacy-scoped deduplication idea: compact and consolidate only within compatible authorization/provenance boundaries, and retain separate views where audiences differ.

### 5. Explicit context budgets and retrieval telemetry

**Source mechanisms:** [retrieval telemetry](/home/thaddus/SimpleMem/EvolveMem/evolvemem/manager.py:274), [token packing](/home/thaddus/SimpleMem/EvolveMem/evolvemem/manager.py:4390), and [cross-session context injection](/home/thaddus/SimpleMem/cross/context_injector.py:116).

**Adaptation:** Record which retrieval view contributed each source ID, which requested part it supports, what was omitted due to the budget, and the actual input token/call/time cost. Keep diagnoses and hidden-fact existence information operator-only.

A context count is not a reliable token limit. Budget complete evidence units with source IDs, including rendering overhead; avoid truncating a dose, condition, or negation mid-fact. The inspected manager's packer admits the first unit even if it exceeds the limit, and the cross-session helper estimates tokens using whitespace words. Borrow the budgeting principle and enforce your own measured bounds.

The manager cache key contains scope/query/link mode, rather than your full authorization and lifecycle state. Do not copy it as an answer/permission cache. Your existing transaction-scoped checks and release revalidation remain necessary after expiry, revocation, deletion, or membership changes.

## Relationship to the observed V19 failures

| Recorded example | Earliest confirmed problem | What retrieval evolution can contribute |
|---|---|---|
| Nadia's home BP plan | Ordinary question generated invalid activity ID; subject hold blocked already-bound facts | Diagnose the hold; ranking changes cannot override it. Validate existing V20 recovery first. |
| Tess's Tuesday schedule | Service acceptance selected another worker's grant; repair discarded permission intent | Identify the missing grant precisely. Query expansion must not create it. |
| Owens's medication recap | List explicitly excluded by narrow scope; stopped-medication fact had a pending review | Distinguish exclusion from backlog; improve ranking only after facts become correctly authorized. |
| Elena's allergy plus schedule | Failed conditional erasure produced a subject-wide hold | Repair lifecycle targeting/timing safely; wider retrieval cannot clear the hold. |

This is why the first AutoResearch capability should be **failure-stage diagnosis**, before numerical retrieval tuning. Treating every `no_memory` as a signal to increase top-k would spend tokens without addressing these causes.

Your V20 already separates successful sections and bounds recovery. Its current behavior needs measured evaluation rather than reimplementation under an EvolveMem label.

## Behaviors to exclude from the adaptation

### Benchmark annotations and reference answers in inference

[The benchmark runner](/home/thaddus/SimpleMem/EvolveMem/evolvemem/evolution.py:898) reads dataset category IDs to select strategies. [The LoCoMo adapter](/home/thaddus/SimpleMem/EvolveMem/evolvemem/benchmarks/locomo.py:106), when its category-5 MCQ option is enabled, constructs an option from `adversarial_answer` metadata. It also instructs the model to ignore person-name differences when looking for a matching topic.

These are concrete evaluation-protocol choices in the inspected code. They should not enter your raw-stream runtime. GateMem query type, attack type, expected action, gold answers, and future source turns remain outside inference. Use observable query features for any strategy selector. Do not assume this checkout's scores transfer to a privacy task with a different information boundary.

### Entity-swap and forced answers

[Entity-swap retrieval](/home/thaddus/SimpleMem/EvolveMem/evolvemem/multi_retriever.py:709) removes person names for a benchmark-specific search path. [_verify_answer](/home/thaddus/SimpleMem/EvolveMem/evolvemem/evolution.py:1204) can instruct the model to replace “Unknown” with a likely answer from context.

For your system, the desired correction is: answer a supported part from exact authorized evidence. It is not: guess when an answer is unavailable. Correct privacy/deletion refusals and missing facts must remain possible. Any query expansion stays inside the original policy/subject boundary.

### Incomplete promotion and replay criteria

The [promotion criteria](/home/thaddus/SimpleMem/EvolveMem/evolvemem/promotion.py:7) focus on overlap, specificity, focus, value density, grounding, coverage, and empty retrieval counts. They do not provide your answer/context/deletion leakage and latency gates.

[Replay](/home/thaddus/SimpleMem/EvolveMem/evolvemem/replay.py:291) creates managers against the configured store and scores retrieved material against logged queries/responses/next states. The shown path is not a reconstruction of each query's historical authorized source prefix. It also uses retrieval paths that update access statistics. For your causal evaluation, use isolated disposable copies or snapshots, reconstruct the original prefix, keep the incumbent unaffected, and keep future/logged reference content evaluator-only.

The main evolution loop repeatedly evaluates the supplied QA set to select candidates. Those questions function as development data during search. Retain a separate untouched evaluation set, preferably split by episode/user/task rather than highly correlated adjacent turns. Strong matched baselines and an equal-budget simple parameter sweep are needed to establish that LLM diagnosis adds value.

### Scope strings as sufficient authority

The standalone multi-view retriever accepts a memory list and question without your authenticated context. The manager's `MemoryQuery` carries a scope string, while scope grant/check APIs are separate. These inspected components are not substitutes for your kernel. This observation is not a complete security audit of every SimpleMem integration.

## Combined AIM + EvolveMem direction

Proposed architecture:

1. A portable service boundary authenticates users, agents, connectors, and processing routes.
2. The existing authority/lifecycle kernel determines which exact source-backed facts are eligible.
3. A versioned retrieval strategy ranks and packs eligible facts under a fixed budget.
4. Evidence-backed generation and final live checks control the released answer.
5. An offline optimizer consumes protected diagnostics, proposes bounded retrieval candidates, and evaluates them under strict utility, privacy, deletion, and latency constraints.

AIM informs steps 1–2's service framing, while your kernel supplies the finer permissions AIM's owner/public rule lacks. SimpleMem informs compact views and multi-view retrieval. EvolveMem informs step 5's experiment loop. The existing kernel remains authoritative across all three.

## Suggested order of work

1. Preserve the four V19 examples as development regressions and assign ledger-grounded failure stages. Do not reinterpret every absent answer as retrieval failure.
2. Measure the existing V20 fixes on matched causal inputs, with source and answer latency reported separately and answer/context/deletion leakage preserved.
3. Externalize only the current ranker's numeric strategy choices into a closed validated configuration. Pin it to each new run's manifest; preserve all prior runs.
4. Compare a few cheap candidates: current fusion versus RRF; facet allocation; duplicate suppression and complete-unit packing. Start with no extra foreground model calls.
5. Build the offline diagnose/propose/replay/history loop. Keep an experiment compute budget and untouched holdout; do not launch automatic promotion or model calls merely as part of this review.
6. Add a two-agent shared-service demonstration and privacy-scoped consolidation after the core regressions and ranking experiments are understood.

A possible research question is: **Can retrieval strategies adapt to diagnosed failures while preserving an independently enforced privacy/lifecycle boundary and a measured latency budget?** This combines the useful ideas without claiming that self-tuning alone solves authorization inference, that finite tests prove privacy universally, or that the whole architecture is novel relative to all prior work.
