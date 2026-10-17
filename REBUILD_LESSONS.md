# Rebuild lessons: Auto Research → gsm-rebuild → shm_evolve

Compiled 2026-10-16 from the three attempts in this repo, their run directories, docs and the
project memory notes. Purpose: one place that says what we tried, what each attempt measured, what
broke, and what the next build must get right the first time.

Sources: `Auto Research/runs_ours/README.md`, `Auto Research/runs_ours/NOTES_harness_2026-09-27.md`,
`Auto Research/docs/self_correction.md`, `gsm-rebuild/README.md`, `gsm-rebuild/docs/GATEMEM.md`,
`gsm-rebuild/results/{four_domain_scores,continuous_setting}.md`, `shm_evolve/DESIGN.md`,
`shm_evolve/runs/*/history.jsonl`, `RESEARCH_OVERVIEW.md`, `docs/RESULTS.md`.

---

## 0. The short version

1. **The best measured system is still hand-built GSM v20auth/v22hist** (mean answer-level MGS 0.538/0.539
   over four domains, #1 on office, #2 elsewhere behind long_context; best strict MGS everywhere).
   Neither automated loop has beaten it under the adaptive, no-census rules.
2. **The gap to long_context is utility, not leakage.** Our A and F are already at or below long_context.
   Every attempt converged on the same diagnosis: required evidence does not reach the answerer
   (too-narrow audiences, bad retrieval/selection, lost names/references), and the answerer refuses.
3. **The outer loop's measurement is the weakest part of every attempt.** Run-to-run noise
   (±0.03 U, ±0.06 A per domain; ±0.045 MGS on a 12-episode held-out for an almost identical system) is
   as large as the effects we try to detect. shm_evolve's gate accepted an edit whose full-set MGS fell
   0.374 → 0.357 and held-out fell 0.419 → 0.318.
4. **Letting a model rewrite the architecture (Auto Research) was intractable**; **letting it only edit
   four prompts by ≤3 bullets (shm_evolve) was too weak and too slow** (2 accepts in 8 iterations, 2–10 h
   per iteration, one of them a regression). The rebuild needs a middle ground, and the human-engineered
   mechanisms from GSM should be the starting point, not something the loop must rediscover.
5. **Silent evidence bugs blinded the loop several times** (nested debug dict, degraded answerer scored as
   real, judge falling back to rules, model replies rejected by the scaffold into fallbacks). Every
   pipeline stage needs a loud health counter and a halt threshold.
6. **Fix format and contract failures with tests, not with the score gate.** Parse errors, schema
   rejections and budget refusals are engineering bugs; spending a 5-hour probe to "discover" them is waste
   and, as shown, the gate can accept them for the wrong reason.

---

## 1. The task and the fixed facts

### 1.1 Benchmark

GateMem: 91 synthetic episodes, 2,218 checkpoints across four domains. Episodes are 190–260 turns.

| Domain | Checkpoints | Notes |
|---|---:|---|
| medical | 579 (210 utility, 192 privacy, 177 deletion) | every episode has **two** patient principals |
| office | 547 | subject is one `project_id`; turns never use ids, only names ("Maple", nested "Maplemark") |
| education | 540 | |
| household | 552 | |

MGS = U × (1 − A) × (1 − F). Answer-level MGS counts a leak when it is in the final answer.
Strict MGS also counts forbidden content in the answerer's prompt (`bench/eval/scorer.py`,
`compliance_utility_e2e_score`). Over-refusal (OR) is reported separately.

### 1.2 User decisions that stand (do not reopen without asking)

- **Adaptive memory** (2026-09-25): memory must reorganise within an episode as information arrives.
  No dataset census in any brief; no dataset vocabulary in code or prompts (lint-enforced in shm_evolve).
- **Grants only at write time**, never during `query`. The keyring gate is frozen; access *facts* are data;
  only *interpretation* evolves.
- **Runtime memory LLM allowed under a per-episode budget** (`llm.py` frozen).
- **Full benchmark for real runs** (reversed the old 50-checkpoint rule on 2026-09-18).
- **Answerer thinking ON**, to match how every baseline was produced (see §4.2).
- **SoCLaaS spend caps**: $50/day, $500/month. Long jobs must be resumable.
- shm_evolve decisions (2026-10-13): fixed scaffold, prompts-only, closed DDL vocabulary, inner-loop
  signals 1–2 only, Reflector→Curator with paired probe gate. These were for that design; the rebuild
  is the user's call to revisit.

### 1.3 Baselines (Qwen3.8-27B answerer and Qwen3.8-27B judge, thinking at server default = ON)

| System | Medical | Office | Education | Household | Mean answer-level | Mean strict |
|---|---:|---:|---:|---:|---:|---:|
| long_context | **0.766** | 0.582 | **0.566** | **0.583** | **0.624** | 0.000 |
| a_mem | 0.441 | 0.518 | 0.184 | 0.396 | 0.385 | 0.003 |
| rag_naive | 0.460 | 0.469 | 0.194 | 0.373 | 0.374 | 0.003 |
| rag_policy | 0.309 | 0.540 | 0.157 | 0.293 | 0.325 | 0.075 |
| remem_i | 0.351 | 0.414 | 0.127 | 0.297 | 0.297 | 0.005 |
| mem0 | 0.303 | 0.318 | 0.295 | 0.234 | 0.288 | 0.013 |
| remem_s | 0.298 | 0.349 | 0.061 | 0.180 | 0.222 | 0.010 |

long_context U/A/F: medical 0.919/0.161/0.006, office 0.831/0.281/0.027, education 0.639/0.094/0.022,
household 0.728/0.196/0.005. Its prompt holds forbidden content on 99–100% of privacy/deletion checkpoints.

Judge matters: under GPT-4.1 the same long_context medical run scores 0.867 (U 0.910, A 0.047), not 0.766.
Always compare on the same judge.

---

## 2. The three attempts

### 2.1 Auto Research (2026-09-17 → 09-28): a LangGraph loop that writes the memory system

**What it was.** Architect (design + DDL + work order) → Developer (self-driving tool loop that writes
code) → Evaluator (sharded, field-stripped) → Judge (only reader of labels) → Critic (attribution) →
back to Architect. Agents on DeepSeek via OpenRouter; evaluator/answerer on local Qwen.

**Trajectory (best dev MGS per try, selected highlights).**

| Try | What changed | Medical | Office |
|---|---|---:|---:|
| 06 | v3 pipeline, medical-specific seed | 0.703 | – |
| 08 | generic template (no domain words) | 0.135 | 0.546 |
| 09 | denial-saturation evidence to the Critic | 0.558 | 0.258 |
| 13 | pipeline fixes, architect reasoning budget, probe, resume | 0.568 | 0.411 |
| 15 | frozen grant rule in keyring | 0.497 | 0.558 |
| 18 | **no seed, task template + data census**, thinking ON | **0.825** | **0.751** |
| 19 | **adaptive task**: no census, grants only at ingest, budgeted memory LLM | 0.190 | 0.538 |
| 20 | adaptive task + harness fixes C0–C11 | 0.418 | 0.302 |

**Why try 18 does not count.** It fitted role tables from the census (expected action by role is label
statistics), and its champions called `keyring.grant` at query time right before `open_for`, so the gate
was a formality. When the user removed both, scores collapsed (try 19). Nothing from try 18 transfers.

**What it taught (kept for the rebuild):**

- **Lineage must follow the best, not the last.** One run regressed 0.317 → 0.119 over five iterations
  because each iteration inherited the previous code, and the halt reason reported the last score as
  "best". Fix: an append-only score history, champion = best judged iteration, failed builds never parents.
  Also keep near-ties (`LINEAGE_TIE_EPS`): a mechanism that is 0.003 below the champion but uses the memory
  model correctly was being erased.
- **The Critic needs "which term moved", not "which term is largest".** Under MGS with small A and F, the
  marginal ranking names U every time, which is algebra, not a finding. `term_deltas` and an explicit
  `revert` verdict fixed that.
- **Relevance before policy (gate 0).** Policy-denying irrelevant records first made "denied list non-empty"
  true for nearly every query, which downgraded correct answers to `answer_redacted`. Testing relevance
  first raised U 0.28 → 0.78 on the dev slice (0.23 → 0.51 on 579) with A and F bit-identical.
- **Group failures by requester, not just by mechanism.** Medical capped at 0.135 because the agent kept
  only the last of two patients as `subject_id`; 86/86 patient utility checkpoints were denied. The Critic
  saw "denied_scope" 108 times and never crossed it with role. The denial-saturation table (fail rate
  ≥ 0.95 with an empty allow-set, per requester role, no corpus margin) is what surfaces it.
- **Names, not ids.** Office turns never contain the project id (0 occurrences); they say "Maple" 186×,
  and names nest ("Maple" ⊂ "Maplemark"). Medical has 281 full-name mentions. Any memory that matches ids
  verbatim gives nobody grants. Medical's loop found name resolution at iteration 3 (+0.2); office's never did.
- **Held-out mechanism checks catch what contract tests miss.** 18 synthetic checks (consent, revocation,
  supersession, reference-by-name, nested names) × paraphrases × unseen vocabularies, run outside the
  workspace. A 50% overall threshold let a build through with **zero** passes on both name families;
  the gate must require ≥ 1 pass in every family. The first name family was also passable without
  resolving names (the holder spoke the fact), so check that each synthetic check actually forces the
  mechanism.
- **Leak channel matters.** All 75 leaks in harness-A iterations 3–4 had records released, mostly under
  `answer_redacted`: the memory handed partially-trusted principals the records and relied on the answerer
  to summarise. Report "leaked via released evidence" vs "leaked with nothing released".
- **Developer budgets.** Episodes that hit the turn ceiling were 76–87% `read_file`. A visible turn counter
  plus a "land it now" directive worked better than raising the ceiling. Work-order size predicted
  failure (10 steps → ceiling, 5 steps → built).
- **Why it was retired.** Every iteration could produce a different architecture, so no score delta was
  attributable, and most of the run budget went to building, not learning.

### 2.2 gsm-rebuild (≈2026-10-01 → 10-07): a hand-engineered governed memory service

**What it was.** Postgres 16 + pgvector with forced row-level security, immutable versioned memory atoms,
deterministic policy engine (RBAC + ReBAC + ABAC + grants/denials), retrieval only inside a pre-authorized
set, schema projections (full / redacted / aggregate / existence), release verifier, tombstones and an
idempotent deletion cascade. Qwen labels each turn; trusted code decides what the label means.

**Results (Qwen judge, all four domains, benchmark answerer).**

| Version | Medical | Office | Education | Household | Mean answer-level | Mean strict |
|---|---:|---:|---:|---:|---:|---:|
| v13 | 0.641 | 0.588 | 0.370 | 0.420 | 0.505 | **0.315** |
| v20auth (repairs R3–R6 + scope-fit) | 0.624 | **0.613** | 0.426 | 0.490 | 0.538 | 0.299 |
| v21own (+activity notes, own activity) | 0.655 | 0.585 | 0.409 | 0.511 | 0.540 | – |
| v22hist (+delete history) | 0.662 | 0.570 | 0.448 | 0.476 | 0.539 | 0.290 |

v13's strict MGS is ~4.2× policy RAG's. Each repair widened what the answerer sees: answer-level MGS rose,
strict MGS fell slightly, and prompt exposure is still 18–48% (privacy) and 9–33% (deletion).

**Mechanisms that measured positive (reuse these):**

- **Verbatim quotes, not model restatements.** Each turn is split into ≤ 6 exact quotes, verified against
  the message. A version that stored model paraphrases dropped the wording later questions reuse and named
  patients in every record, which made the answerer refuse staff more.
- **Completeness guard.** If a quote is not verbatim, or a protected value is in no quote, also store the
  whole message, so splitting never loses a value.
- **Owners see their own information; merely mentioned people get nothing automatically.** Adding everyone
  mentioned leaked incident details to a mentioned contractor; adding nobody dropped medical U 0.695 → 0.486.
  Owners are assigned per quote by a separate call.
- **Closed audience menu.** Audiences only from this episode's groups, roles, named principals, "speaker",
  "about", "everyone"; unknown tokens dropped and counted.
- **Supersession needs key match AND content similarity** (calibrated, label-free threshold), because
  model-chosen keys are too coarse. A widening supersession by a non-owner retires the old value instead.
- **Re-scoping earlier memories on consent/revocation**, with targets picked only among what the speaker
  can see, and GSM authorizing each change. Denials outrank grants.
- **Deletions as the requester**: governed search as the speaker, model picks targets among results, values
  of deleted memories are masked out of new statements.
- **Utility repairs R3–R6**: `keep_readers` (an update keeps old readers and their update/delete rights),
  `delegate_floor`, `carry_forward` (keep still-valid parts of a superseded statement), `partial_delete`
  (remove only the targeted part), whole-token value masking ("July 2, 2026" no longer retires "2026").
- **Questions are not facts**: a turn that only asks is not stored.
- **Answerer bounded by the memory**: declining is always allowed; content requires permitted evidence and
  passes the release verifier; failures fall back to deterministic rendering of permitted evidence.
- **Separate small auxiliary calls** instead of folding new judgments into the labelling prompt. Folding them
  in shifted unrelated audience decisions (office "everyone" labels 20% → 31%).

**Mechanisms that measured negative (do not re-add without new evidence):**

| Mechanism | Effect |
|---|---|
| need-to-know demotion of oversight/limited groups | office A 0.228 → 0.170 but MGS office 0.571 → 0.431, medical 0.637 → 0.530 |
| oversight-group value withholding | 11 utility checkpoints lost vs 3 leaks fixed |
| revocation overrides ownership | hid patients' own limiting statements from family; leaks rose |
| scope-fit on medical/office | neutral within noise at ~5,000 extra calls per run (useful on education/household) |

**Continuous setting (no reset between episodes).** long_context barely moves (0.624 → 0.645 mean).
GSM v13 drops 0.505 → 0.448, almost all from office (0.588 → 0.344, U 0.747 → 0.429): office principals
recur across 17 episodes, each asker can read a median of 622 records (max 2,338), which exceeds the
400-record / 60k-character full-context budget, so 342/547 checkpoints fell back to ranked top-20 and lost
required facts. **Retrieval that scales with readable memory size is an unsolved requirement.**

**Leak analysis.** Half of education deletion leaks are the answerer saying "Yes." to confirm-the-deleted-value
probes with no evidence at all. That is an answerer behaviour, not a memory one.

**Why it was not the end point.** It is static: humans engineered every mechanism, and the user wants a
memory whose organisation adapts within an episode plus a loop that improves it. Postgres was heavy to run.

### 2.3 shm_evolve (2026-10-13 → 10-16): fixed scaffold, prompts-only evolution

**What it was.** Pipeline front_door → queue → processor → librarian → store, retriever at query time.
Closed op vocabulary, frozen record envelope, closed DDL vocabulary for structure changes, frozen keyring gate.
Outer loop: evaluate → judge → attribute each failure to (component, op, outcome) via traces → Reflector
picks one component → Curator makes ≤ 3 bullet edits → paired win/loss probe gate → append-only releases.
Plain Python, sqlite3, httpx; 180+ tests with a deterministic fake LLM. All models on SoCLaaS.

**Runs.**

| Run | Scope | r0 (seed prompts) | Outcome |
|---|---|---|---|
| office_r0 | office, 17 episodes | MGS 0.405 (U .500 A .164 F .032) | iter 1 accepted (retriever search fix): **0.436** full; iters 2–3 rejected |
| joint_v1 | office + medical | office 0.262 | broken by an operator-contract fix that let bad model filters empty candidate sets |
| joint_v2 | 26 train / 12 held-out | train 0.374, held-out 0.375 (office .474, medical .286) | iter 1 rejected on margin 13–11 although probe MGS rose |
| joint_v3 | same + batched librarian | train 0.374, held-out 0.419 (office .481, medical .363) | iter 1 curator ran out of tokens; iter 2 rejected; **iter 3 accepted, then full 0.357 and held-out 0.318**; process gone mid-iter 4 |

Baselines on the office judge family for comparison: long_context 0.600, a_mem 0.500, rag_policy 0.464,
mem0 0.311. shm_evolve's best office (0.436) is below GSM v20auth office (0.613).

**What the loop found that was real.** The r0 retriever's model-composed searches were rejected 39% of the
time (`where` on fields the schema lacks, `'eq'` instead of `=`), so it fell back to an unfiltered "candidate
soup". The fix gave +0.031 on the full office set. The Reflector found it from traces after a different edit
was rejected, so trace attribution works.

**Where the latest champion fails** (joint_v3 r0, 26 train episodes, 186 failures):

| Failure | Count |
|---|---:|
| utility attributed to retriever `select` | 83 |
| privacy attributed to retriever `select` | 20 |
| privacy attributed to processor `add` (audience too wide at write) | 16 |
| utility attributed to front_door `fact` / processor `add` | 27 |
| safety: processor `delete` found no target | 7 |

`family_member` is a saturated requester group (every utility check fails, nothing released).
Over-refusal is 0.30. Utility failures are 130 of 186.

**Health signals that were ignored by design or by accident:**

- Librarian rejected 23% of its own decisions; seed prompt declined 83% of structure proposals.
  `integrity:schema_mismatch` fired 4,421 times per evaluation. Structure evolution produced mostly noise.
- Front door had 270 "reply has no chunks list" rejections per evaluation, each silently degrading the chunk
  to a speaker-private fact through the fallback. That is a parse bug; it should have been fixed by a test
  and a retry, not by a 9-hour gated iteration.
- Read-time signals (738 denied, 450 empty retrievals per evaluation) were logged and deliberately not fed
  back. Empty retrieval is directly the utility failure we most need to fix.

**Why it is not enough.**

- **The gate is noise-limited.** office_r0's probe overstated the accepted gain (+0.115 on probe vs +0.031
  full). joint_v3 accepted a front-door format edit on paired probe MGS 0.264 → 0.268 (19 wins / 15 losses),
  then lost 0.017 on the full train set and 0.101 on held-out. With the same seed prompts and a small
  librarian change, held-out moved 0.375 → 0.419 between joint_v2 and joint_v3.
- **The probe is biased.** It mixes seeded episodes with promoted champion failures; champion probe U was
  0.36 vs 0.48 on the full set, so probe deltas do not predict full-set deltas.
- **Coordinate ascent on prompts is slow.** 2 accepts over 8 iterations across all runs; an iteration
  costs 2–10 hours; one accept was a regression.
- **Prompts cannot fix mechanism gaps.** The levers GSM found (owners per quote, keep_readers, carry_forward,
  partial delete, supersede-by-similarity, name resolution) are code-level mechanisms. A bullet edit to a
  prompt cannot add them.

---

## 3. Cross-cutting lessons about the optimisation loop

1. **Decide the measurement before the system.** Fix: train / held-out split by episode from day one
   (pinned in the run manifest), the judge, the answerer config, and the noise floor. Measure the noise
   floor first: run the same champion 3× on the probe and the full set and record the spread per metric.
2. **Gate on the full training set with a paired test, not on a small promoted probe.** Pair by
   checkpoint, use a bootstrap or sign-test confidence interval on paired MGS, and require the lower bound
   to clear 0. Check held-out only on accept, and reject if held-out falls beyond its noise band. If a full
   evaluation is too expensive to gate on, the per-iteration cost is the thing to cut (see §5).
3. **Never put a pure engineering fix through the score gate.** Parse/contract rejection rates, budget
   refusals, timeouts and fallback rates are health metrics with fixed thresholds. Fix them with tests and
   retries before any experiment runs.
4. **Every stage must report its own degradation loudly**, and the run must halt or mark the score
   ineligible above a threshold. Seen so far: answerer down for a whole run (7,496 connection errors) while
   the fallback pasted raw evidence and scored **higher** (0.837); agent debug nested one level too deep so
   the Critic saw `allowed=?` for every checkpoint for two whole tries; judge silently on the rule
   fallback because of `MOCK_MODE`/transport defaults; 731–800 memory calls per evaluation refused over
   budget, all on late turns where revocations happen.
5. **Champion = best judged release; failed builds are never parents; report best and final separately.**
6. **Attribute failures to a decision, by requester role, and by leak channel.** Mechanism buckets alone hid
   the two-patient bug. Requester-role saturation and "released vs nothing released" are the two cuts that
   found real bugs.
7. **Keep evidence diagnostics in data, not in prompts.** The user's constraint: Architect/Critic/Reflector
   prompts stay free of domain instructions; new diagnoses go into the evidence tables.
8. **Held-out mechanism checks are worth having**, with every family required to pass at least once and
   each check verified to force its mechanism.
9. **Bounded edits and attributable changes are good; edit units that cannot express mechanisms are not.**
   Auto Research failed by being unbounded; shm_evolve by being too narrow.

---

## 4. Lessons about the benchmark and the data

### 4.1 Data quirks

- Medical: two patient principals per episode (family pairs). Any single-scalar `subject_id` breaks.
- Office: ids never appear in turns; references are by (nested) name. Medical refers to people by full name.
- Office `office_episode_custom_en_009_keystone_keystream_dual_project` `ckpt_01` (as of t060) expects
  "August 19, 2026" / "244,000 USD" although turns t046/t048 already superseded them. long_context fails it
  too. Expect one unavoidable office utility miss; do not fit to it.
- Relationship records are heterogeneous (`principal_id`/`clinician_id`/`family_id` subjects;
  `project_id`/`patient_id`/`for_principal_id`/`manages`/... objects) and carry scope attributes
  (`access_scope`, `access_level`, `scope`).

### 4.2 Answerer and judge

- **All baselines ran with Qwen thinking ON** (server default; the bench client never sends
  `enable_thinking`). Our evaluator originally forced it off, which handicapped every earlier run. A/B on 27
  office checkpoints our champion hard-refused: thinking off answered 3/27, thinking on 22/27.
- The answerer decides the final action when its JSON parses; the memory's own action is advisory.
- Use `bench/prompts/judge_prompt.txt` (what `bench/scripts/score_predictions.py` uses), and score baselines
  with the same judge. GPT-4.1 and Qwen judges disagree by ~0.1 MGS on the same predictions.
- Noise: about ±0.03 U and ±0.06 A per domain at full-domain size; larger on probes.

### 4.3 Answer-level vs strict

Answer-level MGS rewards handing the answerer everything (long_context leads). Strict MGS rewards keeping
forbidden content out of the prompt (GSM leads; every baseline but policy RAG is near 0). Widening evidence
raised answer-level and lowered strict in every GSM version. **The rebuild must state which one is the
target**, or optimise answer-level subject to a strict floor. Otherwise the loop will drift towards
long_context behaviour.

---

## 5. Operations and cost

| Item | Measured |
|---|---|
| memory-model calls per office episode (shm_evolve, one pass) | ~550 calls, ~1.1–1.3M prompt tokens |
| 26-episode train evaluation (joint_v3) | 13,194 memory calls, 22.3M prompt + 3.5M completion tokens, ~5 h at 5 workers |
| memory-model cost of that evaluation on SoCLaaS | ≈ $16 (at $0.36/M in, $2.19/M out), before answerer and judge |
| 12-episode held-out | ~2.6 h; 9-episode probe ~1.9 h |
| full office evaluation at 3 workers | ~2.8 h |
| Reflector / Curator call | ~200 s / ~110 s; need `OPTIMIZER_TIMEOUT_S` ≥ 900 |
| GSM v13 interpreter call | ~0.3 s thinking off vs ~2.5 s thinking on |

With the $50/day cap, that is two or three full train evaluations per day. Budget the loop accordingly.

Operational rules learned the hard way:

- **Kill runs by PID**, never `pkill -f`/`pgrep -f | xargs kill` with the command text: the tool's own shell
  matches and dies (exit 144). Kill the children first (`pkill -TERM -P <pid>`), confirm with `pgrep`.
- **Kill ProcessPool children before resuming.** SQLite opens its journal by path; an orphaned child wrote a
  journal next to the new child's database and caused `disk I/O error`.
- **Gate relaunches on a pytest exit code written to a file**, not on grepping output.
- **SoCLaaS**: slug `qwen3.8:27b` has no vendor prefix, so prefix-based thinking switches silently never
  fire; rate-limits above ~20 concurrent requests (429s); needs a CA bundle (`SSL_CERT_FILE`). Memory calls
  from episode children bypass the parent's concurrency semaphore; `EPISODE_WORKERS` is the only lever.
- **Retry the right errors**: 429, 5xx, 520–524, 529, and 402 `in_flight_budget_exhausted` with its
  `Retry-After` honoured in full. Treat only true out-of-credit 402 as fatal. A transient error must retry the
  iteration, never end the run.
- **Per-episode timeout must bound running time, not queue time** (13/17 episodes "timed out" otherwise).
- **Reasoning budgets**: an empty reasoning reply needs one retry without reasoning; the curator once failed
  with "completion budget exhausted".
- **Make everything resumable and reuse cached work**: champion evaluations, judge verdicts, episodes from
  aborted iterations. Pin the eval set in a run manifest so a resume cannot change what it measures.

---

## 6. What the rebuild should be: a checklist

These follow from the evidence above. Items marked **(decide)** need the user's call before building.

### 6.1 Scope and target

- [ ] **(decide)** Primary metric: answer-level MGS, strict MGS, or answer-level with a strict floor.
- [ ] **(decide)** Domains in the training loop (office + medical, or all four) and the held-out split.
- [ ] **(decide)** Per-episode only, or also the continuous setting (which demands scalable retrieval).
- [ ] Bar to beat: GSM v20auth per domain (0.624 / 0.613 / 0.426 / 0.490), and long_context on answer-level.

### 6.2 Memory system

- [ ] Start from GSM's measured-positive mechanisms (§2.2) re-implemented on sqlite, not from blank prompts.
      Keep shm_evolve's frozen pieces: keyring gate, envelope, closed op vocabulary, tests and invariants.
- [ ] Verbatim quote storage with the completeness guard; per-quote owners; closed audience menu.
- [ ] Name/reference resolution as a first-class component (names, first names, nested names).
- [ ] Multiple subjects per record and per episode.
- [ ] Relevance before policy; a non-empty denied list must mean "responsive content exists".
- [ ] Supersession by key + content similarity, with carry-forward and keep_readers.
- [ ] Deletion as the requester, partial delete, reaching earlier versions.
- [ ] Retrieval that degrades gracefully as readable memory grows (full context when it fits, otherwise
      thread-first or query-decomposed ranking). Measure evidence recall on utility checkpoints directly.
- [ ] Read-time signals (empty retrieval, denials) available to the adaptation step; they point straight at
      the dominant failure.
- [ ] Every model reply parsed with a schema, retried once on failure, and its rejection rate reported.

### 6.3 What evolves

- [ ] **(decide)** The edit unit. Options seen so far: whole codebase (intractable), four prompts by bullets
      (too weak). A middle ground is a fixed set of named mechanism modules with typed interfaces, where the
      loop may rewrite one module's code or prompt per iteration under its own unit tests.
- [ ] One component per iteration, so deltas stay attributable.

### 6.4 Measurement and gate

- [ ] Measure the noise floor (3 repeats of the champion) before the first iteration.
- [ ] Gate on paired per-checkpoint outcomes over the full train set with a confidence bound; check held-out
      on accept with its own noise band; reject on any per-domain fall beyond noise.
- [ ] No probe built from promoted failures alone; if used, it only pre-screens before the full-set gate.
- [ ] Health metrics with halt thresholds: answerer fallback rate, judge fallback rate, model-reply rejection
      rate per component, budget refusals, episode errors, timeouts.
- [ ] Attribution by decision, by requester role (saturation), and by leak channel.
- [ ] Answerer thinking ON; judge = the benchmark's own `judge_prompt.txt`; baselines rescored on that judge.

### 6.5 Operations

- [ ] Run manifest pinning episodes, seeds, judge, answerer config and code hash.
- [ ] Cost estimate printed before each evaluation; daily-cap aware scheduling; resumable from any stage.
- [ ] One stop script that kills by PID tree and verifies nothing is left.

---

## 7. Open questions carried forward

- Can a model-driven adaptation step match GSM's hand-built repairs at all, or should those repairs be
  frozen into the scaffold and only the remaining utility gap be evolved?
- How much of the remaining utility gap is the answerer refusing with correct evidence present vs. the
  evidence missing? (Thinking ON closed most of the former in one A/B; re-measure on the new system.)
- Is the probe gate salvageable with larger probes and a proper confidence bound, or must every decision use
  the full train set?
- Would a stronger or reasoning-enabled memory model for the hard calls (librarian, retriever select) pay
  for its cost? joint runs enabled thinking on those two; the batched librarian cut episode time 25–30%.
