#!/usr/bin/env bash
# Separate LOCAL judge model. No benchmark data leaves the machine.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$root/runs"
export CUDA_VISIBLE_DEVICES="${AMEM_JUDGE_GPU_IDS:-3}"
export HF_HOME=/home/thaddus/.cache/huggingface
export HF_HUB_OFFLINE=1
unset HF_HUB_CACHE TRANSFORMERS_CACHE
model_path=${AMEM_JUDGE_MODEL_PATH:-/home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3-14B/snapshots/40c069824f4251a91eefaf281ebe4c544efd3e18}
exec /home/thaddus/gatemem/.venv-vllm/bin/vllm serve "$model_path" \
  --served-model-name 'Qwen/Qwen3-14B' \
  --reasoning-parser qwen3 --tensor-parallel-size 1 \
  --dtype bfloat16 --max-model-len 8192 --max-num-seqs 4 \
  --enforce-eager --gpu-memory-utilization 0.90 \
  --host 127.0.0.1 --port 8001 --api-key dummy \
  > "$root/runs/judge-server.log" 2>&1
