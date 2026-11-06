#!/usr/bin/env bash
set -euo pipefail
umask 077
exec /home/thaddus/gatemem/.venv-nb/bin/python -u /home/thaddus/a-mem-variant/runs/gatemem-medical-v21-gpu234-full-20261102T182855+0800/invocations/answer-reference-repair-20261102T201136+0800/resume.py run --resume
