#!/usr/bin/env bash
# Re-judge an existing CIMemories run with the local Qwen3.8-27B (GPU 6, :8003) with thinking on.
#   usage: scripts/rejudge_cimemories_thinking.sh <run_dir>   (copy solutions.jsonl into a fresh run dir first)
# Resumable: run it again after any stop.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/miniconda3/envs/gsm/bin:$PATH"
export LOCAL_JUDGE_KEY="${LOCAL_JUDGE_KEY:-sk-local-51234343261773190d18e399d0accf1f}"
RUN="$1"
echo "=== $(date '+%F %T') judge (thinking) + score: $RUN"
python -m gsm.cli cimemories score --labels outputs/cimemories/labels_qwen38_soclaas.json --run-dir "$RUN" \
  --workers "${WORKERS:-32}" --judge-base-url http://localhost:8003/v1 --judge-model Qwen/Qwen3.8-27B \
  --judge-api-key-env LOCAL_JUDGE_KEY --judge-thinking --judge-max-tokens 16384 || exit 1
echo "=== $(date '+%F %T') done: $RUN"
