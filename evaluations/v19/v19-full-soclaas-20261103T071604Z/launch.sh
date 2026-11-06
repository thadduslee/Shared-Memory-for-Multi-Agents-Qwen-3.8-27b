#!/usr/bin/env bash
set -uo pipefail
umask 077
cd /home/thaddus/v19-evaluations/v19-full-soclaas-20261103T071604Z
python3 -u supervise.py 2>&1 | tee -a run.log
result=${PIPESTATUS[0]}
printf '%s\n' "$result" > exit-code.txt
exit "$result"
