# Stage 2 + erasure fix (commit 072c244): full rejudge with GPT-4.1 via OpenRouter, original benchmark judge

Source run: `/home/thaddus/v19-evaluations/v19-stage2-erasure-full-20261107T155701Z` (21 episodes, 579 checkpoints).
`inputs/official_predictions.jsonl` and `inputs/scores.jsonl` are the per-episode files concatenated in
episode order `_001_`..`_021_`; per-episode manifests and summaries are copied under `inputs/episodes/`.

Same procedure as `v19-original-gpt-4.1-openrouter-20261107T022528Z`: `run_rejudge.py` and `launch.sh` are
byte-identical copies, and `scoring.py`, `rejudge.py` and `processors.py` in `frozen-source/` (this run's frozen
code) are byte-identical to that job's. `frozen-dataset/` is the same GateMem fixtures and auxiliary scorer.
Every original auxiliary score is reproduced before any paid request. This keeps the original semantic
context review of `retrieved_memory`; the strict full-context judge is deliberately not used here.

Model `openai/gpt-4.1`, endpoint `https://openrouter.ai/api/v1`, temperature 0, 2048 output tokens, six
concurrent requests, structured responses with local verdict validation. Failed judgments stay unscored;
no rule-only fallback. The API key is read from `/home/thaddus/a-mem-variant/.env` and not copied.

`manifest.json` (written by `build_manifest.py`) pins every input, frozen file and source-run file by SHA-256;
the runner checks them before and after judging. Run or resume with `bash launch.sh` (tmux session
`v19-stage2-erasure-gpt41`). Outputs: `scores.jsonl`, `summary.json`, `status.json`, `usage.json`,
`errors.json`, `rejudge.log`, durable journal `rejudge.sqlite`.
