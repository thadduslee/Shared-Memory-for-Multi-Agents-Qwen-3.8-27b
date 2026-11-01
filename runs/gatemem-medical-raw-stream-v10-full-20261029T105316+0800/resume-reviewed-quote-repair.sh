#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/thaddus/a-mem-variant
run_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec > >(tee -a "$run_dir/launcher.log") 2>&1
trap 'status=$?; printf "%s\n" "$status" > "$run_dir/exit-status"' EXIT
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 10 --run-dir "$run_dir" --resume --resume-code-revision d44e9d4aecc2bfe7d388ea22ca6ce79ab0a88a23085aff0de80fd9c74ffb1065 --max-boundaries 0
/home/thaddus/gatemem/.venv-nb/bin/python -u analysis/quote-source-repair-v10-20261029/repair_pending_job.py
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 10 --run-dir "$run_dir" --resume
