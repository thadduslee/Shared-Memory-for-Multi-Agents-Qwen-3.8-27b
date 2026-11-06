# V19 rejudge with govmem answer and strict context-exposure scoring

tmux session: `v19-strict-gpu03` (`model` and `rejudge` windows).
Attach with `tmux attach -t v19-strict-gpu03`; detach with Ctrl-b then d.

The rejudge uses the unchanged scoring implementation copied from
`/home/thaddus/a-mem-gov/govmem/src/govmem/eval/judge.py` and its benchmark prompt.
Judge: local Qwen3.8-27B, thinking disabled, GPUs 0 and 3, endpoint port 18003.
Original V19 answers, original exports, and runtime SQLite stores are not modified.

V19 predictions lack govmem's context audit field. `run_rejudge.py` reconstructs
each historical memory packet using its recorded `input_manifest.memory_ids`,
immutable attribution-view/source-span receipts, and the original fixture text.
It checks reconstructed text against all 4,111 surviving referenced records and
1,391 released support spans, and recovers 141 referenced records later erased
from runtime storage. All 4,252 unique records are recovered; no missing context
is treated as empty. Evidence order and source-prefix boundaries are verified.
Context exposure checks the full memory evidence content, excluding the query
and evaluator-only annotations; it does not use final selected support snippets.

Outputs:
- `summary.json`, `scores.json`: full fresh govmem rule + LLM rejudge, normal and strict metrics.
- `verdicts.jsonl`: per-checkpoint verdicts, including context_exposed.
- `strict_on_original_scores.json`: separate extension retaining the original published U/A/F
  and adding govmem's context OR. It is distinct from the fresh full rejudge.
- `inputs/predictions.jsonl`: adapted immutable answers and reconstructed context audits.
- `inputs/context_records.jsonl`, `context_recovery.json`: context recovery proof.
- `manifest.json`, `frozen-govmem/`: hashes, judge revision, source and configuration.
- `rejudge.log`, `model.log`, `status.json`: progress and model startup.
- `rejudge.sqlite`, `judge-cache.sqlite`: durable checkpoint and prompt caches.

Normal govmem scores can differ from V19's original scores because the rule/LLM
merge and judging prompt differ. Within each result, U is unchanged between its
normal and strict metrics. A_strict and F_strict OR answer leakage with exposure;
MGS_strict uses the same U. Unavailable LLM judgments fail rather than silently
publishing rule-only fallback verdicts. Restarting the runner resumes completed
checkpoints and cached model calls. The model window is interrupted automatically
when the runner exits, releasing its GPUs.
