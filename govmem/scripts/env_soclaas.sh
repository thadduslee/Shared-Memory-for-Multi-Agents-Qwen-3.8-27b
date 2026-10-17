#!/usr/bin/env bash
# Sourced, never executed. Every model role on SoCLaaS qwen3.8:27b (memory thinking OFF, answerer ON, judge OFF).
_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$_ROOT/.env"); set +a
: "${SOCLAAS_BASE_URL:?missing in .env}"; : "${SOCLAAS_API_KEY:?missing in .env}"
export SOCLAAS_MODEL="qwen3.8:27b"
export SSL_CERT_FILE="$_ROOT/shm_evolve/runs/soclaas-ca-bundle.pem"
export GOVMEM_MEMORY_THINKING=0 GOVMEM_ANSWER_THINKING=1 GOVMEM_JUDGE_THINKING=0
# SoCLaaS rate-limits above ~20 concurrent requests; memory calls come from each episode process.
export GOVMEM_MEMORY_MAX_CONCURRENCY=4 GOVMEM_ANSWER_MAX_CONCURRENCY=6 GOVMEM_JUDGE_MAX_CONCURRENCY=6
export HF_HOME=/node2/alfred/huggingface HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export GATEMEM_REPO="$_ROOT"
export PYTHONPATH="$_ROOT/govmem/src${PYTHONPATH:+:$PYTHONPATH}"
