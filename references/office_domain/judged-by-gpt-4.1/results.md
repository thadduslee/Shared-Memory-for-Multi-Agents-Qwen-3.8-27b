# Office domain judged by GPT-4.1

All 547 checkpoints across 17 episodes rejudged with `openai/gpt-4.1` through OpenRouter using the regular frozen GateMem scorer.

| Metric | GPT-4.1 | Original judge |
|---|---:|---:|
| U | 64.9351% | 74.6753% |
| A | 14.6199% | 15.2047% |
| F | 4.9550% | 5.4054% |
| MGS | 52.6945% | 59.8984% |
| OR | 9.7403% | 9.7403% |

U and MGS: higher is better. A, F, and OR: lower is better. Rates are pooled across checkpoints. MGS = U × (1 − A) × (1 − F).

Counts: checkpoints: 547; utility_correct: 100/154; privacy_leaks: 25/171; deletion_leaks: 11/222.

All validation checks passed; source answers and auxiliary scores are unchanged. See `validation_and_comparison.json` for per-episode metrics, usage, and validation.
