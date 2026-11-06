#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/thaddus/v19-evaluations/v19-fixed-gpu45-20261103T110826Z
set +e
python3 -u supervise.py 2>&1 | tee -a run.log
result=${PIPESTATUS[0]}
printf '%s\n' "$result" > exit-code.txt
exit "$result"
