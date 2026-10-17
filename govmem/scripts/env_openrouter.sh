#!/usr/bin/env bash
# Sourced, never executed. Memory model and demo agent on OpenRouter. The model is NOT defaulted:
#   export GOVMEM_OPENROUTER_MODEL=<openrouter model id>   before sourcing, or nothing is called.
_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$_ROOT/.env"); set +a
: "${OPENROUTER_API_KEY:?missing in .env}"
_BASE="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
if [[ -n "${GOVMEM_OPENROUTER_MODEL:-}" ]]; then
  for role in MEMORY ANSWER JUDGE; do
    export GOVMEM_${role}_BASE_URL="$_BASE" GOVMEM_${role}_API_KEY="$OPENROUTER_API_KEY" GOVMEM_${role}_MODEL="$GOVMEM_OPENROUTER_MODEL"
  done
else
  echo "env_openrouter: GOVMEM_OPENROUTER_MODEL is not set; memory model and agent stay disabled" >&2
  unset SOCLAAS_BASE_URL SOCLAAS_API_KEY SOCLAAS_MODEL
  export GOVMEM_MEMORY_BASE_URL="" GOVMEM_ANSWER_BASE_URL="" GOVMEM_JUDGE_BASE_URL=""
fi
unset SOCLAAS_BASE_URL SOCLAAS_API_KEY
export GOVMEM_MEMORY_THINKING=0 GOVMEM_ANSWER_THINKING=0 GOVMEM_JUDGE_THINKING=0
export GOVMEM_MEMORY_MAX_CONCURRENCY=4 GOVMEM_ANSWER_MAX_CONCURRENCY=4
export HF_HOME=/node2/alfred/huggingface HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$_ROOT/govmem/src${PYTHONPATH:+:$PYTHONPATH}"
