# Full GateMem run: 579 checkpoints

Attach to the live progress window:

```sh
tmux attach -t gatemem-v16-full-1101
```

Use Ctrl-b, then n to switch between `run` and `progress`; Ctrl-b, then d detaches.
The benchmark continues after detaching.

Files in this directory:

- `run.log`: unbuffered source processing and checkpoint log.
- `progress.json`: live counts and metrics refreshed every 10 seconds; missing rates are unscored, not zero.
- `progress-history.jsonl`: progress history.
- `summary.json`: native coverage, scores, failures and source latency, refreshed after each boundary.
- `scores.jsonl`: individual native checkpoint scores, refreshed after each boundary.
- `official_predictions.jsonl`: immutable checkpoint answers and evidence manifests.
- `model-trace.jsonl`: source/answer model inputs, outputs, token usage, provider and latency.
- `comparison-first25.json`: automatic exact-prefix comparison to the original v15 run once 25 scores exist.
- `exit-status.json`: created when the runner ends; `implementation-audit.json` follows.
- `evaluated-source.tar.gz`: frozen evaluated source and launch helpers, without credentials.

Scope: all 21 episodes, 579 checkpoints, 567 boundaries and 4294 source turns,
including source-only data tails after the final query. Starts from empty grants.
Automatic score-based stopping is disabled at the user's request. Privacy/deletion
leaks and utility failures remain scored while the run continues. The original
`early-stop.json` is historical; invocation records are preserved under `invocations/`.
The same evaluated implementation and all eight original scores are preserved.

All models remain the configured original Qwen, embedding model and judge.
Provider routing uses latency sorting, ignoring deepinfra, coreweave and reka.
Provider differences prevent attributing measured speed solely to the algorithm.
The earlier probes/first 25 checkpoints are development data, not held-out proof.
