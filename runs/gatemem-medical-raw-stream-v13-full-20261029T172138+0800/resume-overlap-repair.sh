#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/thaddus/a-mem-variant
run_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec > >(tee -a "$run_dir/launcher.log") 2>&1
trap 'status=$?; printf "%s\n" "$status" > "$run_dir/exit-status"' EXIT
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 13 \
  --run-dir "$run_dir" --resume \
  --resume-code-revision 2a3e028619f255ef28699115a9e59c914f222ed59bdb625b35c42baae694ae68
