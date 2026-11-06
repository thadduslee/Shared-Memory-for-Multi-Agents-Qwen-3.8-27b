#!/usr/bin/env bash
set -euo pipefail
umask 077
export CUDA_VISIBLE_DEVICES=2,3
export HF_HOME=/home/thaddus/.cache/huggingface
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export OMP_NUM_THREADS=2
export TOKENIZERS_PARALLELISM=false
export PYTHONPATH=/home/thaddus/a-mem-variant/scripts/vllm_schema_support
unset HF_HUB_CACHE TRANSFORMERS_CACHE
exec /home/thaddus/gatemem/.venv-vllm/bin/vllm serve /home/thaddus/.cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 --served-model-name qwen3.8:27b --reasoning-parser qwen3 --structured-outputs-config '{"backend":"guidance"}' --tensor-parallel-size 2 --dtype bfloat16 --max-model-len 262144 --max-num-seqs 2 --max-num-batched-tokens 8192 --enable-prefix-caching --gpu-memory-utilization 0.85 --host 127.0.0.1 --port 38721 --api-key dummy --limit-mm-per-prompt '{"image":0,"video":0}' > /home/thaddus/a-mem-variant/runs/gatemem-medical-v21-gpu234-full-20261102T182855+0800/memory-server.log 2>&1
