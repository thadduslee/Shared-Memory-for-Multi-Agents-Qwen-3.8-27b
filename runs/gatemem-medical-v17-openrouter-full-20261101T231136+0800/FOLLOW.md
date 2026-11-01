Fresh v17 full evaluation.

The runner uses frozen-source, not the changing worktree.
Inspect progress.json, summary.json and run.log. Full completion requires all 579 scores, 4,294 source turns and 567 completed boundaries. Utility/privacy failures are retained; the run does not stop on a score.

Attach to the live tmux session:

```bash
tmux attach -t gatemem-v17-full-20261101T231136+0800
```

Window 0 (`evaluation`) shows source/checkpoint output. Window 1 (`results`) shows coverage, scores and timings. Use Ctrl-b then 0 or 1 to switch; Ctrl-b then d detaches without stopping the run.
