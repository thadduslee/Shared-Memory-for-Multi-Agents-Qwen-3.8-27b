#!/usr/bin/env bash
# Run the CIMemories methods on SoCLaaS qwen3.8:27b, then judge and score each.
# Every stage is resumable: if the provider's daily limit stops it, run this script again.
#   Needs: repo-root .env (SOCLAAS_*), the local PostgreSQL (scripts/local_pg.sh start), HF cache.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ../.env; set +a
export PATH="${GSM_ENV_BIN:-$HOME/miniconda3/envs/gsm/bin}:$PATH"
export GSM_DATABASE_URL="$(scripts/local_pg.sh url)"
export GSM_EMBEDDINGS_PROVIDER=sentence_transformers HF_HOME="${HF_HOME:-/node2/alfred/huggingface}" HF_HUB_OFFLINE=1
OUT=outputs/cimemories
LABELS="${LABELS:-$OUT/labels_qwen38_soclaas.json}"
W="${WORKERS:-12}"
EP=(--answerer-base-url "$SOCLAAS_BASE_URL" --answerer-model "$SOCLAAS_MODEL" --answerer-api-key-env SOCLAAS_API_KEY)
JP=(--judge-base-url "$SOCLAAS_BASE_URL" --judge-model "$SOCLAAS_MODEL" --judge-api-key-env SOCLAAS_API_KEY)

run() {  # name, method args...   (ONLY=<name> runs just that method)
  local name="$1"; shift
  if [ -n "${ONLY:-}" ] && [ "$ONLY" != "$name" ]; then return 0; fi
  echo "=== $(date '+%F %T') answers: $name"
  python -m gsm.cli cimemories run --out "$OUT/$name" --trials 5 --workers "$W" "${EP[@]}" "$@" || return 1
  echo "=== $(date '+%F %T') judge + score: $name"
  python -m gsm.cli cimemories score --labels "$LABELS" --run-dir "$OUT/$name" --workers "$W" "${JP[@]}" || return 1
}

run full_context_l1 --method full-context --privacy-level 1 || exit 1
run gsm_purpose --method gsm-purpose --purpose-cache "$OUT/purpose_cache.jsonl" || exit 1
run gsm_retrieval_k20 --method gsm-retrieval --top-k 20 --no-relevance-bar || exit 1
run gsm_purpose_task --method gsm-purpose-task --purpose-cache "$OUT/purpose_cache.jsonl" || exit 1
echo "=== $(date '+%F %T') all methods done"
