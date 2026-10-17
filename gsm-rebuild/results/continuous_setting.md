# Continuous setting: one memory per domain, never reset

GSM v13 and long_context on all four GateMem domains, with memory **not** reset between episodes. Each domain is
replayed as one conversation (episodes in file order, which is chronological) and every checkpoint is answered at
its point in that stream. Same answerer (benchmark `BaseMemoryAgent._run_llm`, Qwen3.8-27B, temperature 0.2,
16,384 output tokens, thinking at the server default) and the same Qwen3.8-27B judge as the per-episode results in
`four_domain_scores.md`. Run 2026-10-06/07 on GPU 6; numbers in `outputs/continuous/continuous_scores.json`
(`analysis/continuous_scores.py`).

## Setup

* **GSM v13:** `gsm gatemem run --continuous` (`src/gsm/eval/gatemem/continuous.py`) with every later repair
  disabled and scope-fit off; otherwise v13's settings (full-context budget 400 records / 60,000 characters, ranked
  top-20 with query decomposition beyond it). The two always-on value-masking fixes from R6 remain.
  Merging the episodes of a domain: turn ids get the episode's position as a prefix; principals and relationships
  are the union (a recurring principal id is one person; medical has 6 ids whose role or name differs between
  episodes, first occurrence kept).
* **long_context:** `experiments/continuous_long_context.py`. For each checkpoint, the most recent turns up to and
  including it that fit the context: 65,536 − 16,384 output − the rest of the prompt − 512 margin ≈ 47.5k tokens,
  counted with the Qwen tokenizer. A whole domain is 77k–197k tokens, so the window slides for most checkpoints
  (medical 399/579, office 419/547, education 448/540, household 451/552).
* **Errors:** long_context office had 11 empty model responses (`llama response missing text`); after two reruns, 4
  remain, all with prompts within ~80 tokens of the full window, and they count as refusals. GSM had none after a
  fix: a revocation in office named 52 principals and one RestrictAccess operation takes at most 50, so restrictions
  are now sent in batches of 50 (that crash ended the first office attempt; all four GSM runs were restarted).

## Headline MGS (answer-level)

Continuous:

| System | Medical | Office | Education | Household | Mean |
|---|---|---|---|---|---|
| long_context | 0.729 | 0.592 | 0.648 | 0.611 | 0.645 |
| GSM v13 | 0.557 | 0.344 | 0.474 | 0.418 | 0.448 |

Per-episode (reset at each episode, for comparison):

| System | Medical | Office | Education | Household | Mean |
|---|---|---|---|---|---|
| long_context | 0.766 | 0.582 | 0.566 | 0.583 | 0.624 |
| GSM v13 | 0.641 | 0.588 | 0.370 | 0.420 | 0.505 |

## Strict MGS (answer leak OR forbidden content in the answer model's prompt)

Continuous:

| System | Medical | Office | Education | Household | Mean |
|---|---|---|---|---|---|
| long_context | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| GSM v13 | 0.387 | 0.262 | 0.136 | 0.212 | 0.249 |

Per-episode:

| System | Medical | Office | Education | Household | Mean |
|---|---|---|---|---|---|
| long_context | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| GSM v13 | 0.443 | 0.409 | 0.175 | 0.231 | 0.315 |

## Components, continuous (utility / privacy leak / deletion leak)

| System | Medical U / A / F | Office U / A / F | Education U / A / F | Household U / A / F |
|---|---|---|---|---|
| long_context | 0.914 / 0.198 / 0.006 | 0.831 / 0.275 / 0.018 | 0.711 / 0.083 / 0.006 | 0.750 / 0.185 / 0.000 |
| GSM v13 | 0.629 / 0.104 / 0.011 | 0.429 / 0.187 / 0.014 | 0.544 / 0.056 / 0.078 | 0.478 / 0.125 / 0.000 |

## What changed and why

* **long_context barely changes** (mean headline 0.645 continuous vs
  0.624 per episode). The sliding window keeps the most recent ~47.5k
  tokens, which usually still covers the current episode, and the answer prompt's access policy still keeps its answer-level
  leaks moderate. Its strict score stays 0: forbidden content is in its prompt on 98–100% of privacy and deletion checkpoints.
* **GSM drops in office** (headline 0.588 → 0.344, utility
  0.747 → 0.429). Office is the one domain where the same people recur across episodes
  (53 principals over 17 episodes), so each asker can read a median of 622 records (max 2,338) once memory is never reset. That
  exceeds GSM's 400-record full-context budget, so 342 of 547 office checkpoints fell back to ranked top-20 retrieval, and required
  facts were missing from the evidence on most failed utility checkpoints. In the other domains each asker's readable memory stays
  small (median 47–136 records) and full context is used for (almost) every checkpoint.
* **GSM rises in education** (headline 0.370 → 0.474) and is flat in household; medical drops
  0.084. long_context also rises in education (+0.083), so part of that change is
  likely run-to-run variation of the answerer and judge (about ±0.03 U and ±0.06 A per domain); the continuous runs also re-label
  every turn under a domain-wide roster.
* **GSM still leads the strict score in every domain** (mean 0.249 vs 0.000), but by less than
  per episode, mainly because of office.
* **What would fix office:** a retrieval budget or ranking that copes with large per-asker memories, e.g. raising the
  full-context budget toward the answerer's context size, or ranking within the current conversation thread first.
