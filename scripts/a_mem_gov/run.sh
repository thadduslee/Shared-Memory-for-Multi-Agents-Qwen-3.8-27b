#!/usr/bin/env bash
# Run a_mem_gov on one GateMem domain with the benchmark's own runner and answerer, resuming until complete.
#
#   scripts/a_mem_gov/run.sh <domain> [run_name] [extra run_eval.py args...]
#
# Endpoint (OpenAI-compatible; SoCLaaS by default, or a local vLLM):
#   AMG_API_BASE      default $SOCLAAS_BASE_URL         e.g. http://localhost:8000/v1
#   AMG_API_KEY_ENV   default SOCLAAS_API_KEY           name of the env var holding the key
#   AMG_MODEL         default qwen3.8:27b               e.g. Qwen/Qwen3.8-27B on vLLM
# Run settings:
#   AMG_FEATURES      default chrono,asker,evolve,gate  add policy,owner for the per-episode role defaults
#                                                        and the owner gate (both cost utility; see README)
#   AMG_CONCURRENCY   default 3                         episodes in parallel; SoCLaaS rejects >~12-16 calls
#                                                        in flight in total, a dedicated vLLM takes far more
#   AMG_RETRIES       default 20
#   AMG_OUT           default outputs_amem_gov
#   AMG_PYTHON        default python                    env with numpy, requests, torch, transformers
# Answers use the server's default thinking (on for Qwen3.8, as in every baseline); the per-turn
# memory-write calls send enable_thinking=false.
set -u
DOMAIN=${1:?usage: run.sh <domain> [run_name] [extra args]}; shift
NAME=${DOMAIN}_qwen38_27b_a_mem_gov
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then NAME=$1; shift; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
[ -f .env ] && { set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env); set +a; }

API_BASE=${AMG_API_BASE:-${SOCLAAS_BASE_URL:-}}
: "${API_BASE:?set AMG_API_BASE (or SOCLAAS_BASE_URL in .env)}"
KEY_ENV=${AMG_API_KEY_ENV:-SOCLAAS_API_KEY}
[ -n "${!KEY_ENV:-}" ] || export "$KEY_ENV"=EMPTY  # local vLLM without --api-key
MODEL=${AMG_MODEL:-qwen3.8:27b}
OUT=${AMG_OUT:-outputs_amem_gov}
PY=${AMG_PYTHON:-python}
N=$(grep -c . "bench/data/$DOMAIN/checkpoints.jsonl")

# MiniLM embeds on CPU in every run; without a cap, parallel runs thrash a many-core box.
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-8} MKL_NUM_THREADS=${MKL_NUM_THREADS:-8} TOKENIZERS_PARALLELISM=false
[ -n "${SSL_CERT_FILE:-}" ] && export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
mkdir -p "$OUT"

for attempt in 1 2 3 4; do
  "$PY" bench/scripts/run_eval.py --config configs/runs/paper_main.yaml \
    --data_dir "bench/data/$DOMAIN" --agent a_mem_gov --run_name "$NAME" --out_dir "$OUT" \
    --llm_provider llama --llm_model "$MODEL" --api_base "$API_BASE" --api_key_env "$KEY_ENV" \
    --temperature 0.2 --max_output_tokens 16384 --timeout_s 900 --max_retries "${AMG_RETRIES:-20}" \
    --embed_provider hf --embed_model sentence-transformers/all-MiniLM-L6-v2 --embed_device cpu \
    --top_k 20 --resume --no_progress --episode_concurrency "${AMG_CONCURRENCY:-3}" \
    --a_mem_gov_features "${AMG_FEATURES:-chrono,asker,evolve,gate}" \
    --a_mem_gov_write_cache "$OUT/write_cache_${NAME}.jsonl" "$@"
  got=$(wc -l < "$OUT/$NAME/predictions.jsonl" 2>/dev/null || echo 0)
  echo "$(date +%T) $DOMAIN attempt $attempt: $got/$N checkpoints"
  [ "$got" -ge "$N" ] && exit 0
done
echo "$DOMAIN incomplete after 4 attempts" >&2
exit 1
