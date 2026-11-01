#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
cd /home/thaddus/a-mem-variant
exec > >(tee -a /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v7-full-20261028T162018+0800/console.log) 2>&1
trap 'run_exit=$?; printf "%s\n" "$run_exit" > /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v7-full-20261028T162018+0800/exit-status.txt; printf "Full v7 evaluation exited with status %s\n" "$run_exit"' EXIT
printf '%s\n' 'Full v7: 21 episodes / 4,294 turns / 567 boundaries / 579 checkpoints; evolution enabled.'
/home/thaddus/gatemem/.venv-nb/bin/python -u -m evaluation.gatemem_medical --env-file /home/thaddus/a-mem-variant/.env --preflight
/home/thaddus/gatemem/.venv-nb/bin/python -u -m evaluation.gatemem_medical --env-file /home/thaddus/a-mem-variant/.env --permission-mode inference --permission-version 7 --run-dir /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v7-full-20261028T162018+0800
