#!/usr/bin/env bash
set -euo pipefail
umask 077
export CUDA_VISIBLE_DEVICES=0,3
export HF_HOME=/home/thaddus/.cache/huggingface
export HF_HUB_OFFLINE=1
export PYTHONPATH=/home/thaddus/v19-evaluations/medical_domain_new_system/frozen-source/scripts/vllm_schema_support
unset HF_HUB_CACHE TRANSFORMERS_CACHE
exec /home/thaddus/gatemem/.venv-vllm/bin/vllm serve \
  /home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
  --served-model-name 'qwen3.8:27b' --reasoning-parser qwen3 \
  --structured-outputs-config '{"backend":"guidance"}' \
  --tensor-parallel-size 2 --dtype bfloat16 --max-model-len 262144 --max-num-seqs 4 \
  --enable-prefix-caching --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image":0,"video":0}' \
  --host 127.0.0.1 --port 18000 --api-key dummy
