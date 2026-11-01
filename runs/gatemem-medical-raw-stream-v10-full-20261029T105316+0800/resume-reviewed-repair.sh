#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/thaddus/a-mem-variant
run_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec > >(tee -a "$run_dir/launcher.log") 2>&1
trap 'status=$?; printf "%s\n" "$status" > "$run_dir/exit-status"' EXIT
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 10 --run-dir "$run_dir" --resume --resume-code-revision b93f430cbd088310576155ace27746bd41e2830e4bba47a2356388d89515935d --max-boundaries 0
/home/thaddus/gatemem/.venv-nb/bin/python -u analysis/restriction-review-repair-v10-20261029/repair_pending_job.py
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 10 --run-dir "$run_dir" --resume
