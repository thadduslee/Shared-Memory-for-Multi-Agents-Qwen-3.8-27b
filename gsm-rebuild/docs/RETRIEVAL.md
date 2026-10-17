# Retrieval, disclosure, and existence leakage

## Pre-filtering in PostgreSQL

Authorization happens **before** ranking, in two stages. Neither stage reads a content column.

**Stage A: SQL superset** (`retrieval/candidates.py`). One statement selects atoms that:

* belong to the context's tenant **and** workspace (application predicate, plus forced RLS on
  `current_setting('gsm.tenant_id')`);
* are `lifecycle_state = 'active'` with unexpired `retain_until`;
* have a version valid at the requested time (`JOIN LATERAL` on `memory_versions`, never `tombstoned`);
* are not covered by an active explicit denial (memory, compartment or subject);
* are reachable by the requester through at least one of:
  * ownership (`owner_ids @> [principal]`),
  * rule tags (`access_tags && requester_tags`),
  * existence tags (`existence_tags && requester_tags`, used only for REFUSE ranking),
  * an active memory grant.

`access_tags` are computed at write time from each rule's most selective condition: `rel:member_of:project:apollo`,
`rel:manager_of:principal:dana`, `principal:dana` for `self`, `role:hr`, or `any` for tenant-wide rules.
Requester tags come from trusted roles and the live relationship snapshot. Because a rule can only allow a
requester who holds its tag, Stage A is a superset of what the engine allows. A unit test enumerates rule
shapes to check this. All predicates are GIN-indexed.

**Stage B: exact decision.** The engine evaluates each Stage A row. Only `ALLOW` / `ALLOW_REDACTED` rows become
candidates. Derived memories that inherit policy are admitted in later rounds (up to depth 4), and only when
**every** provenance parent is already authorized. `DENY` rows with existence acknowledgement are kept aside as
*refusable*, and `ESCALATE` rows as *escalations*. Neither ever reaches ranking over content.

## Ranking inside the authorized set

`retrieval/search.py` joins `memory_projections` to an explicit list of authorized `(version_id, view)` pairs
(`unnest(...)`) and scores:

```
idf(t)   = ln((N + 1) / (df_t + 1)) + 1        over the requester's AUTHORIZED projections only
coverage = Σ idf(query terms the item contains) / Σ idf(all query terms)
relevant ⇔ (coverage ≥ GSM_RELEVANCE_MIN_COVERAGE (0.2) ∧ similarity ≥ GSM_VECTOR_SUPPORT_SIMILARITY (off: −1))
           ∨ similarity ≥ GSM_VECTOR_MIN_SIMILARITY (0.35)
score    = w_lex · coverage + w_vec · max(1 − cosine_distance, 0)
keep     = relevant items with score ≥ GSM_RELEVANCE_RELATIVE_CUTOFF (0.5) × best score,
           identical texts collapsed, ORDER BY score DESC, memory_id ASC, LIMIT top_k
```

**Why a relevance bar.** Treating any shared word as relevance made one generic term ("customer",
"project") enough to put an item in front of the answerer, and the planner answered whenever anything was
retrieved. Rare terms now carry the weight, and weak matches are dropped when a strong one exists. Term
weights are computed over what the requester may see, never over hidden records, so the bar cannot become a
side channel. `tests/integration/test_retrieval_and_disclosure.py::TestRelevanceBar` checks all three
properties.

**Semantic support.** With a real sentence embedder, a lexical match can also be required to be at least
somewhat similar in meaning (`GSM_VECTOR_SUPPORT_SIMILARITY`), which rejects items that share a word but not a
topic. The query is embedded as written; the stopword-free tokens are only for the lexical side. The
support floor is off by default because the hashing fake embedder has no semantics.

### Budgeted full context for answering

When everything a requester may see fits a budget (`GSM_FULL_CONTEXT_MAX_ITEMS`, `GSM_FULL_CONTEXT_MAX_CHARS`;
off when items is 0), the answer path receives the whole permitted set, oldest first, instead of a ranked
subset. Governance is unchanged: the set is exactly the authorized `(version, view)` pairs, with the same views.
Only relevance filtering is skipped. Searches (e.g. deletion and re-scope targets) stay ranked.

Why: on GateMem medical, per-question relevance filtering was the main utility loss. Short quoted memories
lose the framing words questions reuse ("medication plan"), and a policy-following answerer also needs the
records that show the *requester's* standing (their own care role and involvement), which a question-focused
ranking never selects. Replaying the baselines' answerer with everything GSM permitted (median 15 records,
at most 177) raised medical U from 0.33 to 0.63 with A not higher (0.16 → 0.12, within answerer noise). When
the set exceeds the budget, ranking applies as below.

### Multi-part questions

A question that asks for several things dilutes coverage: "who is the vendor, which room, and when is demo
day?" has many terms, so an item that fully answers one part covers little of the whole and falls below the
bar. A planner may split the question into atomic parts (`QueryPlan.parts`; the model-backed
`DecomposingPlanner` in `retrieval/decompose.py`, `GSM_PLANNER_PROVIDER=decompose`). Retrieval then:

1. ranks the whole question and each part separately, each with its own IDF weights, bar and relative cutoff,
   all inside the same authorized pairs;
2. merges the lists round-robin (the i-th best of every part before the (i+1)-th of any), skipping repeats and
   identical texts, up to `top_k`.

The decomposer sees only the requester's question, never memory content, and it cannot widen access: parts
are ranked over exactly the pairs the policy engine authorized. Failures, short questions and single-part
answers fall back to the whole question. Decompositions are cached (`GSM_PLANNER_CACHE_PATH`).
`TestMultiPartQuestions` covers the dilution, the per-part bar, the shared `top_k` and authorization.

* **Per-view index material.** Each version has one projection row and one embedding *per view*. A requester
  with a redacted view is ranked on the redacted text, so a query for a hidden value ("knee surgery") cannot
  make a redacted record rank.
* **Exact similarity over the subset.** Vector similarity is computed exactly (`<=>`) over the authorized rows.
  An HNSW index (`embeddings_hnsw_256`) exists for large tenants, but GSM never does "global ANN top-k, then
  filter". That would both leak through scores and lose recall.
* **Embedders.** `fake` (feature hashing, deterministic, default), `sentence_transformers` (local, e.g.
  `all-MiniLM-L6-v2`; `GSM_EMBEDDINGS_MODEL`, `GSM_EMBEDDINGS_DEVICE`), `openai_compat`, or `none` for
  lexical-only mode. Embedding failures also degrade to lexical-only per request.
* **Weights.** Default 0.6 / 0.4, validated to sum to 1. Tokens are `[a-z0-9]+` only, so they cannot inject
  tsquery syntax.

## Disclosure views

Projections are **schema-aware and deterministic** (`disclosure/schemas.py`): each schema declares, per view,
the allowed fields and a template rendered from those fields only.

| schema `availability` | fields | example text |
|---|---|---|
| `full` | employee, availability, return_date, reason, team | Dana Reyes is unavailable until 2026-10-20. Reason: knee surgery recovery. |
| `redacted` | employee, availability, return_date, team | Dana Reyes is unavailable until 2026-10-20. |
| `aggregate` | availability, team | team member unavailable (team: apollo), counted by the generator |
| `existence_only` | none | An availability update exists. |

The registry validator requires views to **nest**: each less-revealing view's fields must be a subset of the
next one's. It caught a real bug during development: an existence-only view that named the employee sat below
an aggregate view that did not, so capping a view downward would have revealed more.

`DisclosurePlan` lists, per item, the view, the allowed fields, the prohibited **field names** and the prohibited
**semantic categories** (e.g. `medical`). It never lists values. In public responses, aggregate items carry no
memory id or timestamps.

### Adding a disclosure view or schema

1. Add a `ContentSchema` to `SCHEMAS` with all four views, each a `ViewSpec(fields, template)`. Categorize
   sensitive fields (`FieldSpec(category=...)`) so the verifier can check categories.
2. The import-time `validate_registry()` rejects non-nesting views.
3. To add a fifth view kind, extend `View` and its rank, add the view to every schema, and review `cap_view`
   and the `release_audits` / `allowed_views` checks in the migration.

## REFUSE versus NO_MEMORY, and existence leakage

| Situation | Action |
|---|---|
| relevant permitted evidence, all full | `ANSWER` |
| relevant permitted evidence, some limited | `ANSWER_REDACTED` |
| nothing answerable, but step-up or a declared purpose would make a relevant record available | `ESCALATE` |
| nothing answerable, and a relevant record exists whose policy allows acknowledging it **to this requester** | `REFUSE` |
| everything else, including "a protected record exists that you may not learn about" | `NO_MEMORY` |

Existence is protected by making the protected case **byte-identical** to the empty case:

* **Pre-filter.** Records the requester has no path to never enter Stage B, so they cannot influence ranking,
  relevance, or the action.
* **Refusal ranking.** REFUSE relevance is computed only for records whose policy acknowledges existence to
  this requester, and only on their existence-only label.
* **Identical responses.** A NO_MEMORY response always has the same text, reason code (`NO_USABLE_MEMORY`)
  and empty evidence. A test compares the protected and empty cases field by field.
* **Uniform not-found.** Direct lookups, updates, grants, deletions and receipts return `NOT_FOUND` for
  missing, cross-tenant, deleted and unauthorized ids alike.
* **Write paths.** Semantic identity is namespaced by owner, so writes never collide with invisible records.
  Deletion selectors count only memories the caller may delete.
* **ESCALATE.** Only produced when the requester would be allowed after step-up, so it reveals nothing they
  are not entitled to.
* **Audit.** Internal audit rows keep the true reasons; they are admin-only.

Known residual channels: response latency, and aggregate counts over very small groups. See
[SECURITY.md](SECURITY.md).
