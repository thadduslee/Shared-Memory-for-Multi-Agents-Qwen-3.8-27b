#!/usr/bin/env bash
# shm_evolve on the SoCLaaS endpoint, no local GPU. Sourced, never executed.
#
#   set -a; source .env; set +a          # SOCLAAS_* (+ SOCLASS_GLM_MODEL)
#   source scripts/serve/env_soclaas_shm.sh
#   cd shm_evolve && python -m loop.run --domain office ...
#
#   memory model, answerer, judge   qwen3.8:27b     SoCLaaS   (vllm / openai routes)
#   reflector, curator              $SOCLASS_GLM_MODEL  SoCLaaS   (openai route)
#
# One shared, rate-limited endpoint serves every role, so the concurrency
# numbers below are the politeness budget, not a throughput target. The
# memory-side calls come from the episode child processes through the FROZEN
# memory_system/llm.py, which has NO retry: a 429 there becomes a cached None
# and the component falls back to its deterministic rule. That failure is
# visible only as `fallback` outcomes in attribution.json, so EPISODE_WORKERS
# is kept low on purpose.

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a; [[ -f "$_REPO_ROOT/.env" ]] && source "$_REPO_ROOT/.env" 2>/dev/null; set +a
: "${SOCLAAS_API_KEY:?SOCLAAS_API_KEY missing (expected in $_REPO_ROOT/.env)}"
: "${SOCLAAS_BASE_URL:?SOCLAAS_BASE_URL missing (expected in $_REPO_ROOT/.env)}"
: "${SOCLASS_GLM_MODEL:?SOCLASS_GLM_MODEL missing (expected in $_REPO_ROOT/.env)}"

_QWEN="${SOCLAAS_MODEL:-qwen3.8:27b}"

# ---- answerer: vllm route, thinking ON (the baselines ran that way) ----
export VLLM_BASE_URL="$SOCLAAS_BASE_URL"
export VLLM_API_KEY="$SOCLAAS_API_KEY"
export EVALUATOR_MODEL="$_QWEN"
export EVALUATOR_THINKING=true
# Reasoning shares this budget; 4096 was measured as all reasoning and no
# content on medical prompts with the local server.
export EVALUATOR_MAX_TOKENS="${EVALUATOR_MAX_TOKENS:-16384}"
# The bare slug: without `qwen3` in this list the thinking-disable never fires
# for the judge (see scripts/serve/env_soclaas.sh for the measurement).
export VLLM_THINKING_MODEL_PREFIXES="Qwen/,qwen/,qwen3"

# ---- memory model: the frozen llm.py reads these directly ----
export MEMORY_LLM_BASE_URL="$SOCLAAS_BASE_URL"
export MEMORY_LLM_API_KEY="$SOCLAAS_API_KEY"
export MEMORY_LLM_MODEL="$_QWEN"
# Measured live: ~550 calls / ~1.1M prompt tokens per 240-turn episode without
# reasoning. Reasoning on the librarian and the retriever's select stage
# (~90 calls per episode) adds a few hundred to a few thousand completion
# tokens each, hence the larger token budget.
export MEMORY_LLM_TOKEN_BUDGET="${MEMORY_LLM_TOKEN_BUDGET:-2500000}"
export MEMORY_LLM_CALL_BUDGET="${MEMORY_LLM_CALL_BUDGET:-900}"
export MEMORY_LLM_THINKING_COMPONENTS="${MEMORY_LLM_THINKING_COMPONENTS:-librarian,retriever:select}"
export MEMORY_LLM_THINKING_MAX_TOKENS="${MEMORY_LLM_THINKING_MAX_TOKENS:-4096}"
# The librarian now decides a BATCH of structure ops per call (mean 3, up to 8),
# and reasoning grows with the batch: single-op calls had p90 ~2000 tokens, so
# a 2048 cap truncated half of the batched calls (joint_v3 attempt 1). 6144
# keeps truncation to the tail while the per-call overhead is still paid once.
export MEMORY_LLM_THINKING_MAX_TOKENS_LIBRARIAN="${MEMORY_LLM_THINKING_MAX_TOKENS_LIBRARIAN:-6144}"

# ---- held-out: 6 episodes per domain are scored per release and never
# attributed, probed or promoted (the number to put next to the baselines).
export HELDOUT_EPISODES_PER_DOMAIN="${HELDOUT_EPISODES_PER_DOMAIN:-6}"

# ---- judge: openai route at the same endpoint; is_vllm is inferred from the
# non-OpenAI hostname, so the thinking-disable applies to qwen3.8:27b here too.
export JUDGE_ROUTE=openai
export OPENAI_BASE_URL="$SOCLAAS_BASE_URL"
export OPENAI_API_KEY="$SOCLAAS_API_KEY"
export JUDGE_MODEL="$_QWEN"
export JUDGE_MAX_TOKENS="${JUDGE_MAX_TOKENS:-2048}"
export USE_LLM_JUDGE=true

# ---- reflector + curator: GLM alias on the same openai route. The alias does
# not match the thinking prefixes, so it keeps its own reasoning, which the
# server returns in `reasoning_content` and the client ignores.
export REFLECTOR_ROUTE=openai
export CURATOR_ROUTE=openai
export REFLECTOR_MODEL="$SOCLASS_GLM_MODEL"
export CURATOR_MODEL="$SOCLASS_GLM_MODEL"


# ---- optimiser calls (GLM alias, reasoning on): one long call is cheaper than
# a retry ladder that starts a fresh generation upstream every 900 s while the
# abandoned ones keep running (joint_v3 iteration 3). Let a call finish, and
# bound a runaway thought by the completion cap instead (~10 min at the
# endpoint's ~40 tok/s).
export OPTIMIZER_TIMEOUT_S="${OPTIMIZER_TIMEOUT_S:-1800}"
export REFLECTOR_MAX_TOKENS="${REFLECTOR_MAX_TOKENS:-24576}"
export CURATOR_MAX_TOKENS="${CURATOR_MAX_TOKENS:-24576}"

# ---- concurrency: one rate-limited endpoint for everything ----
# 5 workers + 8 answerer streams measured clean on 2026-10-14 (rate limits
# absorbed by tools/llm_tap.py's retries), under the ~20 ceiling.
export EPISODE_WORKERS="${EPISODE_WORKERS:-5}"
export VLLM_MAX_CONCURRENCY="${VLLM_MAX_CONCURRENCY:-8}"
export JUDGE_MAX_CONCURRENCY="${JUDGE_MAX_CONCURRENCY:-2}"
# Retry ladder widened for a RATE limit (see env_soclaas.sh): attempts must
# land in later windows, not the same second.
export HTTP_MAX_RETRIES="${HTTP_MAX_RETRIES:-8}"
export HTTP_BACKOFF_BASE_S="${HTTP_BACKOFF_BASE_S:-2.0}"
export HTTP_BACKOFF_MAX_S="${HTTP_BACKOFF_MAX_S:-60.0}"

# ---- loop ----
export GATEMEM_REPO="$_REPO_ROOT"
export RUNS_DIR="${RUNS_DIR:-$_REPO_ROOT/shm_evolve/runs}"
# Noise is ±0.03 U / ±0.06 A and the answerer thinks; a sign test at margin 0
# accepts coin flips.
export MIN_WIN_MARGIN="${MIN_WIN_MARGIN:-1}"
export SHM_FAKE_LLM=0

unset _REPO_ROOT _QWEN

# 2026-10-14 13:44: the server began sending its leaf certificate without the
# ZeroSSL intermediate ("unable to verify the first certificate"). The bundle
# is the system store plus that public intermediate (fetched from the leaf's
# AIA URL, chains to Sectigo's root), so verification stays real. Python's
# urllib (tools/llm_tap.py) honours SSL_CERT_FILE; httpx callers go through
# the tap over plain localhost.
export SSL_CERT_FILE="${SSL_CERT_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/shm_evolve/runs/soclaas-ca-bundle.pem}"
