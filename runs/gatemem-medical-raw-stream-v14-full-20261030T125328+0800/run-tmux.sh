#!/usr/bin/env bash
set -euo pipefail
umask 077
exec /home/thaddus/gatemem/.venv-nb/bin/python -u /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v14-full-20261030T125328+0800/launch.py > /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v14-full-20261030T125328+0800/run.log 2>&1
