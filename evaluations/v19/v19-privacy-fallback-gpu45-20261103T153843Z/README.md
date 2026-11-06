Fresh V19 run with the bounded independent privacy assessment.

Attach: `tmux attach -t v19-privacy-fallback-gpu45-20261103t153843z`

Live run: run.log
Server: memory-server.log
Progress: status.json
Results and cumulative scores: results/summary.json
Detailed predictions and scoring: results/
Privacy assessment outcomes: per-checkpoint processing metrics (`independent_privacy_v19`).

Full schedule: 21 episodes, 567 boundaries, 579 checkpoints, 4294 source turns.
Empty stores; evolution enabled; answer validation enforced; no resume or limits.
Memory and judge use separate calls to the same local Qwen3.8-27B model on GPUs 4 and 5
(serving configuration copied unchanged from the previous V19 job). Local only; no paid API.
Code and input data are frozen; source-hashes.json and workspace.patch record the change.
Validation reports are under validation/.
