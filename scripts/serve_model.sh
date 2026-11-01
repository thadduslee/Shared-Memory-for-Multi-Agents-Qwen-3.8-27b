#!/usr/bin/env bash
# Standalone model service for A-MEM; does not import the old online workflow.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$root/runs"
export CUDA_VISIBLE_DEVICES="${AMEM_GPU_IDS:-4,5}"
export HF_HOME=/home/thaddus/.cache/huggingface
export HF_HUB_OFFLINE=1
# Factor repeated subschemas into equivalent local $refs in both the API and
# engine workers. The unfactored permission schema exceeds llguidance's compiler
# budget as the source prefix grows (v8 first hit it at t078).
export PYTHONPATH="$root/scripts/vllm_schema_support${PYTHONPATH:+:$PYTHONPATH}"
unset HF_HUB_CACHE TRANSFORMERS_CACHE
model_path=${AMEM_MODEL_PATH:-/home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0}
exec /home/thaddus/gatemem/.venv-vllm/bin/vllm serve "$model_path" \
  --served-model-name 'Qwen/Qwen3.8-27B' \
  --reasoning-parser qwen3 \
  --structured-outputs-config '{"backend":"guidance"}' \
  --tensor-parallel-size 2 \
  --dtype bfloat16 \
  --max-model-len "${AMEM_MAX_MODEL_LEN:-262144}" \
  --max-num-seqs 4 \
  --enable-prefix-caching \
  --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt '{"image":0,"video":0}' \
  --host 127.0.0.1 --port 8000 --api-key dummy \
  > "$root/runs/model-server.log" 2>&1
