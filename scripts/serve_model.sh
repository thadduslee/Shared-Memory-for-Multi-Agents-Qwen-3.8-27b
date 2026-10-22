#!/usr/bin/env bash
# Standalone model service for A-MEM; does not import the old online workflow.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$root/runs"
export CUDA_VISIBLE_DEVICES="${AMEM_GPU_IDS:-4,5}"
export HF_HOME=/home/thaddus/.cache/huggingface
export HF_HUB_OFFLINE=1
unset HF_HUB_CACHE TRANSFORMERS_CACHE
model_path=${AMEM_MODEL_PATH:-/home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0}
exec /home/thaddus/gatemem/.venv-vllm/bin/vllm serve "$model_path" \
  --served-model-name 'Qwen/Qwen3.8-27B' \
  --reasoning-parser qwen3 \
  --tensor-parallel-size 2 \
  --dtype bfloat16 \
  --max-model-len 32768 \
  --max-num-seqs 4 \
  --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt '{"image":0,"video":0}' \
  --host 127.0.0.1 --port 8000 --api-key dummy \
  > "$root/runs/model-server.log" 2>&1
