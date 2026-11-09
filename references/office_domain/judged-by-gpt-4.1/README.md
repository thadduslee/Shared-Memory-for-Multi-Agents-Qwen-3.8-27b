# Office domain judged by GPT-4.1

Rejudges all 547 saved office-domain predictions across 17 episodes with the regular frozen GateMem scoring policy, using `openai/gpt-4.1` through OpenRouter. Original answers, fixtures, and auxiliary scoring rules are preserved. Frozen `scoring.py` and `bench/eval/scorer.py` are unchanged copies from this office run.

Configuration: temperature 0, 2,048 output tokens, six concurrent requests, structured verdict validation, and the original semantic review of retrieved memory. Credentials are read from `/home/thaddus/a-mem-variant/.env` without copying them.

`manifest.json` pins the copied inputs, frozen source/dataset, and original source files by SHA-256. Every auxiliary score is reproduced before API requests. Successful judgments are committed individually to `rejudge.sqlite`, allowing a restart to skip them. Up to three passes retry failed checkpoints only.

Outputs: `scores.jsonl`, `summary.json`, `status.json`, `usage.json`, `errors.json`, `validation_and_comparison.json`, and `results.md`. The original episode results remain unchanged.

Run or resume with `bash launch.sh`. The tmux session is `office-gpt41-openrouter`. `status.json` and `rejudge.log` show progress. No local GPU or SOCLAAS model service is started.
