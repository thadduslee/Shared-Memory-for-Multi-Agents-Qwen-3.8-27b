#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd /home/thaddus/a-mem-variant
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
exec > >(tee -a /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800/console.log) 2>&1
trap 'run_exit=$?; printf "%s\n" "$run_exit" > /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800/exit-status.txt; printf "Resumed v8 evaluation exited with status %s\n" "$run_exit"' EXIT
printf '%s\n' 'Resuming v8 after XGrammar service crash; Guidance backend; saved predictions retained.'
/home/thaddus/gatemem/.venv-nb/bin/python -u -m evaluation.gatemem_medical --env-file /home/thaddus/a-mem-variant/.env --permission-mode inference --permission-version 8 --run-dir /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v8-full-20261028T174425+0800 --resume
