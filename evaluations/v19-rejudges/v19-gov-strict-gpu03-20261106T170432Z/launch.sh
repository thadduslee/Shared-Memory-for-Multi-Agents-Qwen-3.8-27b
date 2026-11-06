#!/usr/bin/env bash
set -euo pipefail
umask 077
job=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$job"
unset PYTHONPATH
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=4
trap 'tmux send-keys -t v19-strict-gpu03:model C-c 2>/dev/null || true' EXIT
set +e
/home/thaddus/gatemem/.venv-nb/bin/python -u "$job/run_rejudge.py" 2>&1 | tee -a "$job/rejudge.log"
run_status=${PIPESTATUS[0]}
set -e
printf '%s\n' "$run_status" > "$job/exit-code.txt"
exit "$run_status"
