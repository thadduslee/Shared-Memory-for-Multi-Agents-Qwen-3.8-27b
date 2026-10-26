#!/usr/bin/env bash
set -uo pipefail
umask 077
cd /home/thaddus/a-mem
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export AMEM_LLM_BASE_URL=http://127.0.0.1:8000/v1
export AMEM_LLM_MODEL=Qwen/Qwen3.8-27B
export AMEM_LLM_API_KEY=dummy
export AMEM_JUDGE_BASE_URL=http://127.0.0.1:8001/v1
export AMEM_JUDGE_MODEL=Qwen/Qwen3-14B
export AMEM_JUDGE_API_KEY=dummy
run_dir=/home/thaddus/a-mem/runs/privacy-evolution-off-full-20261025-v2
python_bin=/home/thaddus/gatemem/.venv-nb/bin/python
{
    date -u '+Started at %Y-%m-%dT%H:%M:%SZ'
    echo 'Evolution OFF; fresh full run with exact citation constraints.'
    echo 'Continuing through all 579 checkpoints and 567 boundaries with local models.'
    "$python_bin" -u -m evaluation.gatemem_medical --preflight &&
        "$python_bin" -u analysis/evolution-dependability/resume_with_diagnostics.py \
            --run-dir "$run_dir" --no-evolution
} 2>&1 | tee -a "$run_dir/progress.log"
run_status=${PIPESTATUS[0]}
printf '%s\n' "$run_status" > "$run_dir/exit_code.txt"
printf 'Evaluation exited with code %s. Results: %s/summary.json\n' "$run_status" "$run_dir"
exit "$run_status"
