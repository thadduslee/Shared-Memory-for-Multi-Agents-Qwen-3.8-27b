# Office domain strict benchmark judged by GPT-4.1

Fresh judgments of all 547 saved office-domain predictions across 17 episodes using `openai/gpt-4.1` via OpenRouter and the unchanged scorer in `/home/thaddus/a-mem-gov/govmem/src/govmem/eval/judge.py`. The scorer and benchmark prompt are copied into `frozen-govmem/`; their hashes and the repository revision are recorded in `manifest.json`.

`judge_all()` combines answer rules with fresh LLM verdicts and runs `exposure()` on `output.memory_audit.prompt_context.text`. `aggregate()` reports normal U/A/F/MGS and strict A/F/MGS. U is identical between this benchmark's normal and strict metrics. Its answer prompt and merge differ from the earlier regular GateMem rejudge.

Original predictions lack the govmem audit field. `prepare.py` reconstructs the original recorded memory from immutable attribution history and source spans, checks surviving stored bytes, and verifies each answer's evidence IDs and hashes against `raw-replies.jsonl`. Only memory actually passed to an answer-generation call enters the primary context; answers with no generation call have empty context. `context_recovery.json` documents all checks. The full retrieved packet is retained for a separately reported sensitivity analysis.

Configuration: temperature 0, 2,048 output tokens, six concurrent requests. The unchanged govmem transport supplies OpenRouter access. A response hook audits model identity/provider/usage and rejects model mismatches. The API key is read from `/home/thaddus/a-mem-variant/.env` without being copied. No local model server or GPU allocation is used.

Inputs, code, source memory stores, answers, and audit logs are pinned by SHA-256 and checked before/after scoring. New reply cache and judgment journal are created for this run. Successful judgments are durable and resumable; rule-only fallbacks fail the run.

Run/resume: `bash launch.sh`. Tmux session: `office-gpt41-strict-openrouter`. Outputs include `verdicts.jsonl`, `scores.json`, `summary.json`, `status.json`, `usage.json`, `model_calls.jsonl`, `context_recovery.json`, `validation_and_comparison.json`, and `results.md`.
