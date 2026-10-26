#!/usr/bin/env bash
set -uo pipefail
umask 077
cd /home/thaddus/a-mem
export AMEM_OPENROUTER_JUDGE_MODEL=openai/gpt-4.1
export AMEM_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
run_dir=/home/thaddus/a-mem/runs/privacy-evolution-off-full-20261025-gpt-4.1-openrouter
source_run=/home/thaddus/a-mem/runs/privacy-evolution-off-full-20261025
python_bin=/home/thaddus/gatemem/.venv-nb/bin/python
args=(--source-run "$source_run" --output-dir "$run_dir")
if [[ -f "$run_dir/manifest.json" ]]; then args+=(--resume); fi
{
    date -u '+Started at %Y-%m-%dT%H:%M:%SZ'
    echo 'Independent GPT-4.1 re-judge of 579 saved answers through OpenRouter.'
    "$python_bin" -u -m evaluation.gatemem_medical.rejudge --source-run "$source_run" --preflight &&
        "$python_bin" -u -m evaluation.gatemem_medical.rejudge "${args[@]}"
} 2>&1 | tee -a "$run_dir/progress.log"
run_status=${PIPESTATUS[0]}
printf '%s\n' "$run_status" > "$run_dir/exit_code.txt"
printf 'Re-judge exited with code %s. Results: %s/summary.json\n' "$run_status" "$run_dir"
exit "$run_status"
