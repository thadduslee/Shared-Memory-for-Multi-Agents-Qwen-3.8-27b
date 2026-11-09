#!/usr/bin/env bash
# Serve Qwen3.8-27B with vLLM exactly as the medical run did (runs/medical/serve.sh),
# with the machine-specific parts taken from the environment:
#
#   GPUS        CUDA devices, default 0,3 (GPUs 1, 2, 4 and 5 on the lab machine belong to another user)
#   MODEL_PATH  local model snapshot, default the HF cache snapshot the run used
#   VLLM        vllm executable, default the lab machine's venv, else vllm on PATH
#   PORT        default 18000; API key "dummy" (pipeline/config/model.env.example matches)
#
# Run it in its own tmux window and leave it up; the pipeline waits for it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
umask 077
export CUDA_VISIBLE_DEVICES=${GPUS:-0,3}
export HF_HOME=${HF_HOME:-$HOME/.cache/huggingface}
export HF_HUB_OFFLINE=1
# Schema-reference support used by the structured outputs (see scripts/vllm_schema_support).
export PYTHONPATH=$REPO/scripts/vllm_schema_support
unset HF_HUB_CACHE TRANSFORMERS_CACHE
MODEL_PATH=${MODEL_PATH:-$HF_HOME/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0}
if [ -z "${VLLM:-}" ]; then
  if [ -x /home/thaddus/gatemem/.venv-vllm/bin/vllm ]; then VLLM=/home/thaddus/gatemem/.venv-vllm/bin/vllm; else VLLM=$(command -v vllm); fi
fi
TP=$(( $(tr -cd , <<<"$CUDA_VISIBLE_DEVICES" | wc -c) + 1 ))
exec "$VLLM" serve "$MODEL_PATH" \
  --served-model-name 'qwen3.8:27b' --reasoning-parser qwen3 \
  --structured-outputs-config '{"backend":"guidance"}' \
  --tensor-parallel-size "$TP" --dtype bfloat16 --max-model-len 262144 --max-num-seqs 4 \
  --enable-prefix-caching --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image":0,"video":0}' \
  --host 127.0.0.1 --port "${PORT:-18000}" --api-key dummy
