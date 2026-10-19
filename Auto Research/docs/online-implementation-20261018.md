# Evidence-based online implementation, 18 October 2026

The implementation adds an explicit evidence-based objective alongside the
existing benchmark-driven loop. It preserves benchmark measurements, first
attempts, run snapshots and the existing unrelated `serve_qwen38.sh` change.
No historical run was resumed; no paid model or full benchmark was launched.

## Confirmed findings and corrections

- `_call_answerer` invented `refuse` when a sentences-only response omitted an
  action. It now preserves omission and records model/absent origin. Memory
  continues to own the released action; attempted-action telemetry cannot alone
  create a rendering diagnosis.
- Retrieval's lifecycle status set omitted cancellation even though policy
  rejected inactive resources. Both now share an extensible inactive-state
  definition. Lifecycle exclusions do not enter permission-denial counts or
  create redacted answers.
- Claim selection discarded every facet on a time-role mismatch, lost heading
  context across source fragments, and accepted weak word matches. It now
  normalizes query concepts, checks topic anchors and structural facets,
  preserves provenance-backed heading context and evaluates time facets
  independently. Unknown roles remain distinct from contradictory roles.
- The historical proposal schema failures were real: named safety-check mappings
  were rejected, while correction feedback omitted the useful details. A shared
  schema now normalizes harmless forms, emits safe field/type/guidance errors,
  and gives wire correction two calls within one planner attempt.
- The earlier Critic summarized failed benchmark IDs. It now audits a bounded
  permitted evidence packet and returns causal reports with independently
  checked verification status and explicit uncertainty. Models cannot certify
  defects by confidence or agreement.
- Triggers and promotion previously depended on hidden benchmark pass/fail.
  Evidence mode uses checked invariants without Judge access. Optional benchmark
  measurements are stored separately. A compatibility `passed` field denotes
  checked invariant compliance, with truth/correctness metrics undefined.
- Experiment history previously relied heavily on identity hashes. Bounded JSON
  and Markdown lessons now retain hypotheses, source diffs, predictions,
  measurements, safety results and next checks. Prompt lessons are scoped and
  revalidated. Equivalent failed mechanisms are screened before development,
  with concrete prior-result feedback; diff/replay checks remain backstops.
- Removing assignment was confirmed in boundary 6 candidate workspaces, including
  attempts 3 through 6. Authoritative policy JSON is now immutable to candidates,
  supplemented by frozen negative authorization tests and the independent audit.

The historical run `medical-online-full-20261018-122105` remains an **incomplete
snapshot during boundary 11 of 549**. It supports these implementation findings,
not an experiment-wide performance conclusion.

## Validation

Controlled examples specify expectations independently. Deterministic loop
fixtures simulate a candidate's behavior; they validate orchestration rather
than prove that a model will discover a useful repair.

Completed checks:

- Focused evidence/composition/security/source-view/annotation-wall suite:
  **97 passed** (`ARCHITECT_CODE_VIEW_MAX_CHARS=128000`).
- Existing online workflow and promotion suite: **77 passed**.
- Frozen template invariants: **32 passed**.
- Python compilation and `git diff --check`: passed.

Commands are documented in `online.md`. The focused
suite covers annotation-free execution/promotion, model uncertainty, omission
investigation, schema correction, duplicate avoidance, no-effect early rejection,
protected content, future/hidden-field exclusion, immutable first attempts,
conservative promotion, user feedback, deletion-safe lesson reuse, query
paraphrases, irrelevant additions and explicit replacements. Existing online
workflow tests exercise sandboxed build gates, replay ordering/caching, staged
commit, rollback, failed-candidate counters and immutable history. Template
invariants cover authorization, lifecycle and deletion.

An attempted whole-repository pytest run was interrupted during the existing
mocked offline graph integrations; a whole-suite green result is not claimed.
It also exposed that the configured 120,000-character offline code-view override
no longer fits the expanded template. The default is now 128,000; validation
sets that environment value explicitly without modifying the user's `.env`.

## Compatibility and remaining limits

- `benchmark` remains the default; enable real online evidence-based diagnosis
  explicitly. Legacy missing modes mean benchmark, and a mode change requires a
  new run. Frozen workspaces are preserved rather than patched in place.
- A bounded audit cannot establish completeness outside its scan. Incomplete or
  unvalidated assessments cannot certify promotion. Derived bodies whose raw
  source relationship cannot be checked are withheld from prompts.
- Source truth, usefulness, ambiguous relevance, model-proposed causal stories,
  and events never ingested remain unresolved. Ingestion omissions outside the
  stored source-derived evidence require a separate trusted source adapter.
- The audit intentionally uses the trusted baseline policy/interpretation code;
  supporting a new authoritative interpretation may require a separately
  reviewed audit update. Candidates cannot relax this boundary themselves.
- Evidence-mode regression checks preserve exact established released answers.
  This can reject harmless wording improvements until an independent factual
  equivalence check is supplied.
- Automatic repair is limited to reproducible checked defects. Suspected issues
  receive bounded investigation and discriminating checks, not automatic
  deployment. Independent outcomes need an authenticated application adapter;
  historical replay cannot manufacture real-world candidate outcomes.
- Experiment files inherit local run-directory access controls. Prompt history
  adds requester/episode and current-source checks. An application serving these
  artifacts must supply its own authenticated access-controlled interface.

Before a full experiment, use two fresh small streams with adaptation disabled
and enabled, with identical controlled boundaries and source changes. Compare
immutable first attempts separately from repairs, checked support/coverage,
protected invariants, defect resolution, preserved behavior, unknowns and cost.
Use isolated benchmark measurements only if separately desired and authorized.
No real-world accuracy gain is claimed from these tests or the incomplete run.
