#!/bin/bash
set -uo pipefail
umask 077
cd /home/thaddus/a-mem-variant
export HF_HOME=/home/thaddus/.cache/huggingface
export HF_HUB_OFFLINE=1
export CUDA_VISIBLE_DEVICES=''
export PYTHONPATH=.
export PYTHONUNBUFFERED=1
printf '%s\n' 'Full fresh v15 run: Qwen 3.8-27B through OpenRouter; GPT-4.1 judge.'
/home/thaddus/gatemem/.venv-nb/bin/python -u -m evaluation.gatemem_medical.openrouter --run-dir /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v15-openrouter-full-20261031T180100+0800 2>&1 | tee /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v15-openrouter-full-20261031T180100+0800/run.log
run_status=${PIPESTATUS[0]}
printf '%s\n' "$run_status" > /home/thaddus/a-mem-variant/runs/gatemem-medical-raw-stream-v15-openrouter-full-20261031T180100+0800/exit-code
printf '\nEvaluation exited with code %s. Logs and results are preserved.\n' "$run_status"
exec /bin/bash --noprofile --norc
