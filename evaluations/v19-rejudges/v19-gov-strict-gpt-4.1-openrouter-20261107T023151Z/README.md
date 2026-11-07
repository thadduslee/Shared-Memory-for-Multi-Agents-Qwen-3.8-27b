# Full strict V19 rejudge with GPT-4.1 via OpenRouter

Uses the unchanged frozen govmem scorer and benchmark prompt from
`v19-gov-strict-gpu03-20261106T170432Z`, with byte-identical adapted predictions,
full reconstructed memory contexts, fixtures, and context-recovery proof.
Original answers and all prior run artifacts remain unchanged. No prior model
reply cache or judgment journal is copied; all LLM judgments are fresh GPT-4.1.

Model `openai/gpt-4.1` at `https://openrouter.ai/api/v1`; temperature 0; max_tokens
2048; six concurrent requests. The existing private repository .env supplies the
key. The frozen govmem ChatClient handles OpenRouter. A response hook records
model identity, provider and usage, and rejects a response model mismatch.

The unchanged benchmark merges deterministic answer rules and LLM verdicts, then
computes strict privacy/deletion leakage as answer leakage OR a protected-pattern
match in the full reconstructed model memory context. Utility U is identical in
normal and strict metrics. This benchmark's prompt and merge differ from the
original V19 policy used in the earlier GPT-4.1 original rejudge.

`manifest.json` pins inputs, scorer, prompt, context proof, runner, and settings.
Before scoring, every adapted answer, reconstructed context text, record order,
released support span, and prior strict context-exposure flag is verified.
Original file hashes are checked before and after scoring.

Run or resume with `bash launch.sh`. `rejudge.sqlite` commits each completed
checkpoint, `judge-cache.sqlite` caches fresh GPT-4.1 responses, and `verdicts.jsonl`,
`scores.json`, `summary.json`, `status.json` update after every result. Unavailable
LLM judgments fail the run rather than publish rule-only fallback results.
`model_calls.jsonl` records response metadata; `usage.json` totals reported cost.
Previous scores are copied only for comparison, under `previous_strict_*` and
`original_gpt41_*`. They are never used as new GPT-4.1 verdicts.

tmux session: `v19-strict-gpt41-openrouter`. See `rejudge.log` for progress.
