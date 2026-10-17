# GSM vs GateMem baselines: headline and strict scores

All four GateMem domains (medical 579, office 547, education 540, household 552 checkpoints), all seven baselines, and three
GSM configurations. Every system answers with the benchmark's own answerer (`BaseMemoryAgent._run_llm`, Qwen3.8-27B) and is
judged by the same Qwen3.8-27B judge (`Auto Research/score_baseline.py`). Scores computed 2026-10-05 by
`analysis/strict_scores.py`; raw numbers in `outputs/gatemem/strict_scores/strict_scores.json`.

**Scores.** MGS = U × (1 − A) × (1 − F), where U is utility, A the privacy-leak rate and F the deletion-leak rate.
* **Headline MGS:** answer-level. A leak is counted only when the forbidden content is in the final answer (Qwen judge).
* **Strict MGS:** end-to-end, as defined by GateMem's official scorer (`bench/eval/scorer.py`,
  `compliance_utility_e2e_score`). A leak is counted when the forbidden content is in the answer (Qwen judge) **or** in the
  memory context shown to the answer model (`memory_audit.prompt_context` / `prompt_memory_block`, the scorer's own
  pattern check). Utility is the same as in the headline score.
* **Official rule-based:** the official scorer unchanged (pattern matching for answers too, no LLM judge), for reference.

**Configurations.** Three GSM versions, explained in "GSM versions" below. Bold: best in column.

## Average over the four domains

| System | Headline MGS (mean of 4 domains) | Strict MGS (mean of 4 domains) | Official rule-based MGS (mean) | Official rule-based strict MGS (mean) |
|---|---|---|---|---|
| long_context | **0.624** | 0.000 | **0.524** | 0.000 |
| rag_naive | 0.374 | 0.003 | 0.301 | 0.003 |
| rag_policy | 0.325 | 0.075 | 0.251 | 0.044 |
| a_mem | 0.385 | 0.003 | 0.325 | 0.002 |
| mem0 | 0.288 | 0.013 | 0.232 | 0.012 |
| remem_i | 0.297 | 0.005 | 0.259 | 0.004 |
| remem_s | 0.222 | 0.010 | 0.192 | 0.009 |
| GSM v13 | 0.505 | **0.315** | 0.376 | **0.228** |
| GSM v20auth | 0.538 | 0.299 | 0.371 | 0.200 |
| GSM v22hist | 0.539 | 0.290 | 0.398 | 0.208 |

## Headline MGS (answer-level)

| System | Medical | Office | Education | Household |
|---|---|---|---|---|
| long_context | **0.766** | 0.582 | **0.566** | **0.583** |
| rag_naive | 0.460 | 0.469 | 0.194 | 0.373 |
| rag_policy | 0.309 | 0.540 | 0.157 | 0.293 |
| a_mem | 0.441 | 0.518 | 0.184 | 0.396 |
| mem0 | 0.303 | 0.318 | 0.295 | 0.234 |
| remem_i | 0.351 | 0.414 | 0.127 | 0.297 |
| remem_s | 0.298 | 0.349 | 0.061 | 0.180 |
| GSM v13 | 0.641 | 0.588 | 0.370 | 0.420 |
| GSM v20auth | 0.624 | **0.613** | 0.426 | 0.490 |
| GSM v22hist | 0.662 | 0.570 | 0.448 | 0.476 |

## Strict MGS (answer leak OR forbidden content in the answer model's prompt)

| System | Medical | Office | Education | Household |
|---|---|---|---|---|
| long_context | 0.000 | 0.000 | 0.000 | 0.000 |
| rag_naive | 0.000 | 0.009 | 0.003 | 0.000 |
| rag_policy | 0.173 | 0.049 | 0.049 | 0.027 |
| a_mem | 0.000 | 0.008 | 0.001 | 0.000 |
| mem0 | 0.005 | 0.034 | 0.003 | 0.011 |
| remem_i | 0.005 | 0.006 | 0.003 | 0.004 |
| remem_s | 0.011 | 0.019 | 0.006 | 0.006 |
| GSM v13 | **0.443** | 0.409 | 0.175 | **0.231** |
| GSM v20auth | 0.390 | **0.428** | 0.169 | 0.209 |
| GSM v22hist | 0.391 | 0.381 | **0.176** | 0.214 |

## Components (Qwen judge): utility / privacy leak / deletion leak

| System | Medical U / A / F | Office U / A / F | Education U / A / F | Household U / A / F |
|---|---|---|---|---|
| long_context | 0.919 / 0.161 / 0.006 | 0.831 / 0.281 / 0.027 | 0.639 / 0.094 / 0.022 | 0.728 / 0.196 / 0.005 |
| rag_naive | 0.657 / 0.271 / 0.040 | 0.649 / 0.251 / 0.036 | 0.272 / 0.117 / 0.194 | 0.511 / 0.228 / 0.054 |
| rag_policy | 0.386 / 0.156 / 0.051 | 0.714 / 0.193 / 0.063 | 0.183 / 0.056 / 0.094 | 0.397 / 0.223 / 0.049 |
| a_mem | 0.629 / 0.266 / 0.045 | 0.734 / 0.257 / 0.050 | 0.267 / 0.094 / 0.239 | 0.538 / 0.212 / 0.065 |
| mem0 | 0.462 / 0.328 / 0.023 | 0.448 / 0.246 / 0.059 | 0.383 / 0.083 / 0.161 | 0.310 / 0.196 / 0.060 |
| remem_i | 0.557 / 0.312 / 0.085 | 0.643 / 0.292 / 0.090 | 0.189 / 0.139 / 0.217 | 0.397 / 0.190 / 0.076 |
| remem_s | 0.524 / 0.375 / 0.090 | 0.500 / 0.269 / 0.045 | 0.094 / 0.128 / 0.256 | 0.239 / 0.201 / 0.060 |
| GSM v13 | 0.719 / 0.104 / 0.006 | 0.747 / 0.187 / 0.032 | 0.428 / 0.083 / 0.056 | 0.495 / 0.141 / 0.011 |
| GSM v20auth | 0.681 / 0.068 / 0.017 | 0.766 / 0.170 / 0.036 | 0.494 / 0.061 / 0.083 | 0.560 / 0.120 / 0.005 |
| GSM v22hist | 0.743 / 0.099 / 0.011 | 0.773 / 0.246 / 0.023 | 0.506 / 0.050 / 0.067 | 0.554 / 0.141 / 0.000 |

## Prompt exposure (official scorer): share of privacy / deletion checkpoints whose prompt context holds forbidden content

| System | Medical privacy / deletion | Office privacy / deletion | Education privacy / deletion | Household privacy / deletion |
|---|---|---|---|---|
| long_context | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 0.994 | 1.000 / 1.000 |
| rag_naive | 0.948 / 1.000 | 0.901 / 0.811 | 0.872 / 0.894 | 0.951 / 0.978 |
| rag_policy | 0.333 / 0.209 | 0.520 / 0.829 | 0.317 / 0.594 | 0.679 / 0.755 |
| a_mem | 0.969 / 0.994 | 0.912 / 0.838 | 0.906 / 0.944 | 0.957 / 0.978 |
| mem0 | 0.870 / 0.887 | 0.807 / 0.541 | 0.972 / 0.711 | 0.821 / 0.739 |
| remem_i | 0.880 / 0.921 | 0.877 / 0.878 | 0.778 / 0.900 | 0.783 / 0.940 |
| remem_s | 0.839 / 0.853 | 0.819 / 0.734 | 0.617 / 0.833 | 0.701 / 0.886 |
| GSM v13 | 0.266 / 0.090 | 0.181 / 0.225 | 0.372 / 0.261 | 0.342 / 0.228 |
| GSM v20auth | 0.307 / 0.107 | 0.181 / 0.230 | 0.461 / 0.311 | 0.418 / 0.332 |
| GSM v22hist | 0.318 / 0.164 | 0.193 / 0.243 | 0.478 / 0.306 | 0.413 / 0.310 |

## Official scorer, rule-based only (no LLM judge)

Answer-level:

| System | Medical | Office | Education | Household |
|---|---|---|---|---|
| long_context | **0.388** | **0.600** | **0.583** | **0.523** |
| rag_naive | 0.234 | 0.423 | 0.199 | 0.349 |
| rag_policy | 0.134 | 0.464 | 0.130 | 0.276 |
| a_mem | 0.251 | 0.500 | 0.201 | 0.348 |
| mem0 | 0.136 | 0.311 | 0.286 | 0.196 |
| remem_i | 0.234 | 0.457 | 0.118 | 0.228 |
| remem_s | 0.206 | 0.355 | 0.068 | 0.140 |
| GSM v13 | 0.313 | 0.495 | 0.374 | 0.320 |
| GSM v20auth | 0.240 | 0.527 | 0.411 | 0.307 |
| GSM v22hist | 0.275 | 0.529 | 0.435 | 0.353 |

Strict (end-to-end):

| System | Medical | Office | Education | Household |
|---|---|---|---|---|
| long_context | 0.000 | 0.000 | 0.000 | 0.000 |
| rag_naive | 0.000 | 0.008 | 0.003 | 0.000 |
| rag_policy | 0.071 | 0.042 | 0.039 | 0.025 |
| a_mem | 0.000 | 0.008 | 0.001 | 0.000 |
| mem0 | 0.002 | 0.031 | 0.002 | 0.010 |
| remem_i | 0.003 | 0.007 | 0.003 | 0.003 |
| remem_s | 0.006 | 0.018 | 0.005 | 0.005 |
| GSM v13 | **0.218** | 0.346 | **0.178** | **0.172** |
| GSM v20auth | 0.150 | **0.369** | 0.156 | 0.126 |
| GSM v22hist | 0.161 | 0.358 | 0.161 | 0.152 |

## GSM versions

All three use the same core pipeline, answerer and judge; each adds to the one before.

### GSM v13: the original pipeline

* **Labelling:** Qwen labels each message (kind, exact quotes, a key for changeable values, the audience, who owns the
  information, deletions); owners are assigned per quote.
* **Storage:** a same-key update replaces the old record when the two are similar enough (MiniLM similarity against a
  per-domain threshold); if the update would widen the audience, the old record is retired instead. Consent and
  revocation messages restrict or grant access to earlier records. Questions and chatter are not stored.
* **Answering:** everything the asker may read is handed to the benchmark answerer, oldest first, within a budget of 400
  records and 60,000 characters.
* **Scope-fit off:** groups with a limited scope (logistics-only, scheduling-only) get nothing from records outside their
  audience.

### GSM v20auth = v13 + the utility repairs R3–R6 + scope-fit on

| Repair | Fixes | What it does |
|---|---|---|
| `delegate_floor` | R3 audience too narrow | If a scope-limited delegate is in the audience, the people it acts for are added (e.g. the IT helper's records reach the household) |
| `keep_readers` | R3 | An update keeps the old record's readers, plus its update and delete rights (the "auth" fix that the intermediate run v19rep lacked) |
| `scope_records` | R4 scoped view missing | Each scope-limited group gets its own in-scope version as a separate record |
| `carry_forward` | R5 partial update | Details of the old statement still in effect are appended to the new record |
| `partial_delete` | R6 collateral deletion | Only the targeted part of a record is removed; the rest is re-stored |
| (always on) | R6 | Dates, times and codes count as whole values ("July 2, 2026" no longer retires "2026"); masking replaces whole tokens only |

Scope-fit is switched on for all four domains, so scope-limited groups get in-scope versions of records.

### GSM v22hist = v20auth + the asker's own activity + deletion that reaches history

| Repair | What it does |
|---|---|
| `activity_notes` | A chatter message stating the speaker's own role or task ("I'm reconciling the home medications") is stored, values masked, readable only by the speaker |
| `own_activity` | The answerer also gets the asker's own replaced records, values masked and marked "[earlier, since updated]"; only if the information is still held (not later deleted) and the record held no sensitive values |
| `delete_history` | A deletion request also reaches older versions of records, and deleting a record deletes its older versions |

The intermediate run v21own had `activity_notes` and `own_activity` but not the history and sensitive-value fixes; its
new leaks led to v22hist.

### What the versions trade off

Each step raised utility by widening what the answerer sees: more readers, scoped copies, carried details, the asker's
own history. The same widening puts forbidden content in the answerer's prompt more often, so the four-domain mean
headline MGS rose (0.505 → 0.538 → 0.539) while the strict MGS fell slightly (0.315 → 0.299 → 0.290).

The repair runs also re-label most messages: the labelling prompt lists the memory keys the speaker can see, and the
repairs change those. Part of the variation between versions therefore comes from different labels.

Every repair can be switched off individually with `--disable-repair NAME`; details are in `docs/GATEMEM.md` under
"Utility repairs".

## Notes

* long_context shows the answerer the whole transcript (every episode is at most 260 turns, inside its 300-turn window).
  The answer prompt's own access policy keeps its answer-level leaks moderate, so it leads the headline score in three
  domains; but forbidden content is in its prompt on 99–100% of privacy and deletion checkpoints, so its strict score is 0.
* GSM leads the strict score in every domain. Its own prompt exposure is still 19–48% (privacy) and 16–31% (deletion):
  audiences wider than the benchmark allows, and stale values in records without a key.
* Prompt exposure is pattern matching against `not_include` and `leak_targets`; a few patterns are generic words, which
  inflates exposure for every system alike.
* Run-to-run noise of the answerer is roughly ±0.03 in U and ±0.06 in A per domain.
