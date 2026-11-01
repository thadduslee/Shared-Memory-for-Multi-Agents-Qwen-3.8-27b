#!/usr/bin/env bash
set -uo pipefail
umask 077
cd /home/thaddus/a-mem-variant
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 9 --run-dir /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v9-full-20261029T011945+0800 2>&1 | tee /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v9-full-20261029T011945+0800/launcher.log
evaluation_status=${PIPESTATUS[0]}
printf "Evaluation exited with status %s\n" "$evaluation_status"
printf "%s\n" "$evaluation_status" > /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v9-full-20261029T011945+0800/exit-status
exit "$evaluation_status"
