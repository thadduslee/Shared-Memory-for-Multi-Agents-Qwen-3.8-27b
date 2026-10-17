# Harness notes — overnight 2026-09-27 → 28

Requirements (user): memory must be adaptive; harness general (any domain of this benchmark, no dataset
vocabulary); SQL only. Provider: DeepSeek v4 flash via OpenRouter `deepinfra/fp8` only.

Baseline to beat: try 19 (adaptive task, runs_ours/19_adaptive_task_2026-09-25): medical 0.190 (U .233 A .177
F .011), office 0.538 (U .727 A .187 F .090). Office: 14/20 iterations failed the probe. Memory LLM used by 7
builds, none survived selection; no build used it to read access changes.

## Changes

### C0 — OpenRouter pin: deepinfra/fp8 only
`env_local_eval.sh` exports `OPENROUTER_PROVIDER_ORDER=deepinfra/fp8`, fallbacks off; `config.openrouter_provider_preferences`
now also sends `only: [...]` when fallbacks are off. Verified live: provider = DeepInfra. Tests updated (8 pass).

### C1 — Near-tie lineage (`LINEAGE_TIE_EPS=0.01`)
Champion = best dev MGS (unchanged). Next workspace's parent = most recent comparable build within ε of the best.
Why: try 19 medical iter 15 (memory model reading deletions) scored 0.1866 vs champion 0.1898 and was erased.
Plateau test now documents both settings. Tests pass.

### C2 — Probe tolerance from measured noise
`scoreboard.probe_tolerance` = max(PROBE_TOLERANCE, 2 × median |probe − dev|). Router and probe_fail report use it.
Why: office probe read 0.05–0.37 above dev; 14/20 office iterations (all 4 model builds) never got a full eval.

### C3 — Agent debug was nested one level too deep (BUG, affects all task-template runs 18–19)
The evaluator put the agent's whole debug under `debug.retrieval`, so task agents' counters landed at
`debug.retrieval.retrieval.*` and `memory_llm` at `debug.retrieval.memory_llm`. Consequence: in tries 18–19 the
Critic printed `allowed=?` for every cited checkpoint and the Judge's utility buckets saw n_allowed=0 — the loop was
flying without retrieval evidence. Fixed with `split_agent_debug`; Judge report gets `memory_llm_totals`; Critic
prompt gets a "memory's own model call" block ("not used: …" when zero). Tests pass.

### C4 — Held-out adaptivity checks (gate), memory model in the Developer's test runs
`harness_checks/test_adaptivity.py` (outside every workspace): 12 checks = {consent, revocation, supersession} ×
2 paraphrases × 2 unseen vocabularies, model ENABLED. Run by `run_tests` after the contract suite passes;
`tests_ok` requires ≥ `ADAPTIVITY_MIN_PASS`=0.5. The Developer sees counts + failing *families* (descriptions),
never phrasings. `run_tests`, smoke and evaluator shards all get `config.memory_llm_env()`.
Contract tests no longer force the model off. TASK.md documents the checks and names the lever.
Validation (live, GPU-4 vLLM): keyword-rule reference memory 4/12 (fails gate: consent + revocation families);
model-reading reference memory (classifies each turn as grant/revoke/none → wraps at ingest) 12/12 and 13/13
contract. Both references live in the scratchpad, not in any template.

### C5 — Critic evidence for leaks + generic counters
Critic line prints whatever counters the memory reports (was: seed-only names shown as `?`), plus
`memory_llm_calls`. For each cited privacy leak: up to 3 earlier turns (≤ as_of) that name the asker by id,
display name, or first name — the turns an adaptive memory needed to read. (Episodes refer to people by name,
never by id: medical 281 full-name mentions, office 29 first-name mentions.)

Suite: 743 passed (incl. 20-iteration mock rehearsal).

## Smoke (harness A, all of C0–C5), 00:16–01:10
Build round: 82 turns, 8 retries, gates green INCLUDING the held-out adaptivity gate; the built memory calls
`llm.complete` (1 site). Eval on the 6-checkpoint smoke slice: memory model used on 6/6 checkpoints, 660 calls,
165k tokens, 0 refused over budget, max 219 calls in one replay. MGS 0 on 6 checkpoints (not meaningful).
Cost: ~11 min to evaluate 6 checkpoints in one shard (≈1 model call/s, sequential within a shard). Full dev slices
run 55–58 shards in parallel, so expect ~10–25 min per evaluation; shard timeout is 2400 s.

## Exp A (launched 01:10, 12 iterations, C0–C5) — running
medical iter 1: 0.160 (U .190 A .151 F .011); model used on 579/579 checkpoints (11k calls, 1.45M tokens,
0 over budget); eval ≈ 11 min/pass. Utility buckets: fact_not_released 129, withheld 24, content_missing 17 —
the coverage signal now works (it was silently None in tries 18–19). Patients: 81 failures, 1–3 records released.
medical iter 2: 0.158. office iter 1: 0.290 (U .377 A .199 F .041).

### C6 — Developer turn budget 300 s → 600 s (future launches; Exp A runs keep 300)
Office iter 2's Developer lost two whole turns to timeouts (first sample 150 s, resamples 50 s, 0 chars each).
Measured DeepInfra fp8: 65 tok/s generation; short/12k-token prompts answer in 2–3 s. So it is not latency but
length: rewriting a ~550-line file with reasoning needs >150 s. Build rounds already had 900 s.
office iter 2 (first attempt) died of infrastructure (3 transport timeouts at the 300 s budget); office stopped by PID at 02:28 and resumed from iteration 1 with the 600 s budget (C6) — runs_office_task_harnessA, run.log appended.
medical iter 3 (02:58): **0.362** (U .452 A .182 F .023) — already 1.9× try 19's best (0.190). The Architect targeted
U citing "fact_not_released is 78% of utility failures" (the C3-fixed evidence): store principals' display names in
SQL, resolve names mentioned in turns to principals, and grant the patient-as-subject the records about them.
fact_not_released 129 → 76. Memory model: 10.9k calls, 4.9M tokens, **731 calls refused over budget** (prompts grew
~3×) — the 60k-token/episode budget is starting to bind.
office iter 2: built after a stalled turn (16 turns, 2 retries).

### Exp A status 04:40
| iter | medical MGS (U/A) | office MGS (U/A) |
|---|---|---|
| 1 | 0.160 (.190/.151) | 0.290 (.377/.199) |
| 2 | 0.158 (.190/.156) | 0.288 (.390/.222) near-tie → parent |
| 3 | **0.362** (.452/.182) | 0.283 (.377/.216) |
| 4 | **0.404** (.510/.198) | probe-level only so far |
| 5 | 0.390 (.524/.229) | |
Medical fact_not_released 129 → 58 by iter 4 as the loop acted on the coverage evidence. A rises with U (~0.2 in both):
all leaks have released records; most are `answer_redacted` with evidence attached (medical: family indirect_inference /
overreach / cross_patient; office: sales indirect_inference, delegate/contractor overreach) — the memory hands
partially-trusted principals the records and relies on the answerer to summarise. Memory-model budget binds from
medical iter 3 (731–800 calls refused/eval at 60k tokens/episode; prompts ~3× longer).
Candidate next changes (not applied mid-run): MEMORY_LLM_TOKEN_BUDGET 60k → 120k; Critic line that separates
"leaked via released evidence" from "leaked with nothing released".

### Prepared for the next launch (NOT active in Exp A's processes)
- **C7** `MEMORY_LLM_TOKEN_BUDGET` 60k → 120k per episode. Why: medical iters 3–5 had 731–800 memory calls refused
  over budget per evaluation — the refused calls are the LATE turns of long episodes, where revocations and
  supersessions happen.
- **C8** Judge `leak_channels` (released vs nothing released, × action) → Critic block "HOW PRIVACY LEAKS GOT OUT".
  Why: all 75 Exp-A leaks at iters 3–4 had records released, mostly under `answer_redacted`; the Critic's census
  didn't say so. Test added (68 pass in test_pipeline_fixes).

Exp A 05:25: medical iter 6 **0.406** (U .529 A .214) new best; office iter 5 **0.302** (U .396 A .205) new best.

### C9 — Provider 520s are transient (06:21 incident)
Medical harnessA ENDED at iteration 8 on one Architect call: `HTTP 520 Provider returned error` from DeepInfra.
520 was neither in the client's RETRYABLE_STATUS nor in the router's transient-Architect markers, so a one-off
Cloudflare/origin error was fatal to the run. Added 520–524, 529 to both (+"provider returned error"). Test added.
Suite: 745 passed. Medical resumed from iteration 7 at 06:26 with C6–C9 active (C7's larger memory budget applies
from iteration 8 on; iterations 1–7 ran at 60k). Office continues on its original process (C0–C6 only).
medical iter 7 (before the halt): **0.408** (U .495 A .167 F .011), best so far.
medical iter 8 (07:18, first iteration with C7/C8/C9): **0.418** (U .510 A .162 F .023), new best. Memory model:
11.0k calls, 5.0M tokens, **0 refused over budget** (was 731–800 at 60k). Leak channels (C8): 31 leaks, all with
records released — answer_redacted 15, no_memory 9, refuse 4, answer 3. office iter 6: 0.282, iter 7: 0.287
(best still 0.302 at iter 5).

### Exp A status 08:12 (all dev-slice scores)
| iter | medical MGS (U/A/F) | office MGS (U/A/F) |
|---|---|---|
| 1 | 0.160 (0.191/0.151/0.011) | 0.289 (0.377/0.199/0.041) |
| 2 | 0.158 (0.191/0.156/0.017) | 0.288 (0.390/0.222/0.050) |
| 3 | 0.362 (0.452/0.182/0.023) | 0.283 (0.377/0.216/0.041) |
| 4 | 0.404 (0.509/0.198/0.011) | 0.298 (0.396/0.216/0.041) |
| 5 | 0.390 (0.524/0.229/0.034) | 0.302 (0.396/0.205/0.041) |
| 6 | 0.406 (0.529/0.213/0.023) | 0.282 (0.377/0.216/0.045) |
| 7 | 0.408 (0.495/0.167/0.011) | 0.287 (0.377/0.205/0.041) |
| 8 | 0.418 (0.509/0.162/0.023) | 0.293 (0.377/0.181/0.050) |
| 9 | 0.385 (0.467/0.151/0.028) |  |

### C10 — Held-out family "reference by name" (for the next launch)
Why office is stuck (~0.30, U .38): its memories release only 1–3 records because grants never reach the
records. Office episodes declare objects by id (`project_maple`) but turns NEVER use the id (0 occurrences) —
they say "Maple" (186×), and names nest ("Maple" ⊂ "Maplemark"). Medical got past this at iteration 3 by resolving
display names; office's loop hasn't found it, and none of the held-out checks tested it (they used ids verbatim).
Added 6 checks (3 families × 2 unseen vocabularies): name reaches the holder; nested names not confused; consent
names the grantee by display/first name. Now 18 checks, gate 50%. Scores: keyword memory 6/18 (fails gate), model
memory w/o name resolution 14/18, model memory with word-boundary name resolution 16/18. Developer is told each
failing family's description. Suite 745 passed. Not active in the running processes.
C10 correction (09:20): the first version of the name family let the HOLDER speak the fact, so a
"speaker owns their turn" grant passed it without resolving any name — the office champion scored 17/18. Facts
are now spoken by a fourth principal who holds nothing. Rescored: office champion (iter 5) **13/18, failing both
name families**; its `_extract_subject` matches object ids verbatim, which never occur in office turns → subject
None → no holder grants → n_allowed 5 of 137 on a real checkpoint. Medical champion (iter 8) 9/18 (fails consent
paraphrase + name families). Keyword memory 6/18, model memory 14/18, model + name resolution 16/18.
Plan: when office iter 10 finishes, restart office from its last iteration with C7–C10 (MAX_ITER 16).
office iter 10: 0.291 (U .383 A .205). Office stopped by PID at 09:26 and resumed from iteration 10 with C6–C10, MAX_ITER 16 (suite 745 passed). The next office build must pass ≥ 50% of the 18 held-out checks, including the name families its champion fails.

### C11 — Every held-out family must pass at least once (10:05)
Office iter 11's build passed the 50% gate at 13/18 while scoring ZERO on both name families — the overall
fraction let the one missing mechanism through. Gate is now: ≥ 50% overall AND ≥ 1 pass in every family
(`ADAPTIVITY_EVERY_FAMILY`). Developer sees per-family counts. Verified: name-resolving reference 18/18 PASS;
model reference w/o names 14/18 FAIL (2 zero families); office champion 13/18 FAIL (2 zero families).
Test added; suite 746 passed. office iter 11: 0.286. Office restarted from iteration 11 with C6–C11 (MAX_ITER 16).
medical iter 11: **0.413** (U .486 A .135 F .017) — near-tie with 0.418, lowest A of the run.

## Stopped by the user at 10:59 (2026-09-28)
Both runs stopped by PID (no orchestrator or shard processes left). vLLM on GPU 4 (port 8003) left running.
Final: medical best **0.418** (iter 8; U .509 A .162 F .023) — iteration 12's build (green, 12/18 held-out, old
50% gate) was mid-evaluation and has no score. Office best **0.302** (iter 5; U .396 A .205 F .041) — iteration 12's
Developer (first build under the every-family gate, C11) was mid-episode; whether C11 is passable in practice on
office is untested. Runs stay in runs_medical_task_harnessA / runs_office_task_harnessA (not yet filed).
