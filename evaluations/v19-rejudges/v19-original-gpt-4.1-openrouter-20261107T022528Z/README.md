# Full original V19 rejudge with GPT-4.1 via OpenRouter

Uses unmodified scoring and model transport from the source run's frozen V19
implementation, plus its frozen GateMem fixtures and auxiliary scorer. All 579
original predictions are rescored. Every original auxiliary score is reproduced
before any paid scoring request. This preserves original semantic context review
of retrieved_memory; reconstructed full-context govmem strict judging is deferred.

Model: `openai/gpt-4.1`; endpoint: `https://openrouter.ai/api/v1`; temperature 0;
2048 output tokens; six concurrent requests; structured responses and local
verdict validation. Failed judgments remain unscored. No rule-only fallback.
The private API key is loaded from the repository .env without copying it.

Original exports are read only. `inputs/` holds unchanged copies, `frozen-source/`
and `frozen-dataset/` pin the original code and fixtures. `manifest.json` records
their SHA-256 hashes. The runner verifies hashes both before and after judging.

Run or resume with `bash launch.sh`. Each successful checkpoint is committed to
`rejudge.sqlite`; reruns skip those checkpoints. `scores.jsonl`, `summary.json`,
and `status.json` update after each result. `usage.json` records reported usage
and cost for scoring requests (excluding the separate small availability probe).
`errors.json` records any failed checkpoints. See `rejudge.log` for progress.

tmux session: `v19-original-gpt41-openrouter`.
