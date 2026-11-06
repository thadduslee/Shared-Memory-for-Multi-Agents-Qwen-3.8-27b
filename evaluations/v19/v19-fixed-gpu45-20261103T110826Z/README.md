Fresh V19 rerun with validated context fixes.

Attach: `tmux attach -t v19-fixed-gpu45-20261103t110826z`

Live run: run.log
Server: memory-server.log
Progress: status.json
Results and cumulative scores: results/summary.json
Detailed predictions and scoring: results/

Full schedule: 21 episodes, 567 boundaries, 579 checkpoints, 4294 source turns.
Empty stores; evolution enabled; answer validation enforced; no resume or limits.
Memory and judge use separate calls to the same Qwen3.8-27B model on GPUs 4 and 5.
Code and input data are frozen locally; source-hashes.json and workspace.patch record the fixes.
Validation reports are preserved under validation/.
