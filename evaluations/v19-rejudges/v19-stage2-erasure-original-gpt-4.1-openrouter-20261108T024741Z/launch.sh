#!/usr/bin/env bash
set -euo pipefail
umask 077
job=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$job"
unset PYTHONPATH
export PYTHONUNBUFFERED=1
set +e
/home/thaddus/gatemem/.venv-nb/bin/python -u "$job/run_rejudge.py" >> "$job/rejudge.log" 2>&1
run_status=$?
set -e
printf '%s\n' "$run_status" > "$job/exit-code.txt"
exit "$run_status"
