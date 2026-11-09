# Office domain strict benchmark judged by GPT-4.1

All 547 checkpoints across 17 episodes judged with `openai/gpt-4.1` via OpenRouter using the unchanged `a-mem-gov/govmem/src/govmem/eval/judge.py` and benchmark judge prompt.

| Metric | Answer scoring | Strict context scoring |
|---|---:|---:|
| U | 70.1299% | 70.1299% |
| A | 16.3743% | 18.7135% |
| F | 8.1081% | 27.9279% |
| MGS | 53.8915% | 41.0855% |

Strict leakage is answer leakage OR a protected-pattern match in the reconstructed answering-model memory context. U is shared within this benchmark. Its answer prompt and rule/LLM merge differ from the earlier regular GateMem rejudge.

Contexts were reconstructed from historical source spans and verified against answer audit hashes. Context is empty when generation_calls is zero. There are no unknown contexts.

Counts: utility_correct: 108/154; privacy_answer_leaks: 28/171; deletion_answer_leaks: 18/222; privacy_strict_leaks: 32/171; deletion_strict_leaks: 62/222; privacy_context_exposed: 26; deletion_context_exposed: 56.

Full retrieved-packet sensitivity (including records not passed to generation): A_strict 23.9766%, F_strict 27.9279%, MGS_strict 38.4253%.

All original source-file hashes are unchanged, all verdicts were reproduced from the fresh GPT-4.1 response cache, and no rule-only fallback occurred. See `validation_and_comparison.json` for per-episode metrics and audit details.
