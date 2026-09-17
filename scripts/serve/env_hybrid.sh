# Source this: `source scripts/serve/env_hybrid.sh`
#
#   Architect / Developer / Critic -> OpenRouter, deepseek/deepseek-v4-flash-0731
#                                     (the repo's original setting)
#   Evaluator                      -> local vLLM :8002, Qwen/Qwen3.8-27B (dsh)
#   Judge                          -> local vLLM :8002, Qwen/Qwen3.8-27B (http)
#
# Requires scripts/serve/serve_agents.sh (GPU 1, :8002) and OPENROUTER_API_KEY,
# which is read from the repo-root .env below -- config.py only loads
# "Auto Research/.env", so the root file is NOT picked up on its own.

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -f "$_REPO_ROOT/.env" ]; then set -a; . "$_REPO_ROOT/.env"; set +a; fi
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY missing (expected in $_REPO_ROOT/.env)}"

# ---- agentic nodes: repo defaults, stated explicitly ----
unset AGENT_BASE_URL AGENT_API_KEY_ENV AGENT_ROUTE AGENT_IS_VLLM DSH_BASE_URL DEEPSEEK_BASE_URL DEEPSEEK_API_KEY
unset ARCHITECT_MAX_TOKENS DEVELOPER_MAX_TOKENS CRITIC_MAX_TOKENS
export ARCHITECT_MODEL=deepseek/deepseek-v4-flash-0731
export DEVELOPER_MODEL=deepseek/deepseek-v4-flash-0731
export CRITIC_MODEL=deepseek/deepseek-v4-flash-0731
export AGENT_TRANSPORT=dsh
export DEVELOPER_TRANSPORT=http
export OPENROUTER_REASONING_EFFORT="${OPENROUTER_REASONING_EFFORT:-low}"

# ---- evaluator: local Qwen on the GPU 1 server ----
export VLLM_API_KEY="${VLLM_API_KEY:-sk-local-dummy-key}"
export VLLM_BASE_URL=http://localhost:8002/v1
export EVALUATOR_MODEL=Qwen/Qwen3.8-27B      # prefix required for the thinking-disable
export VLLM_MAX_CONCURRENCY="${VLLM_MAX_CONCURRENCY:-48}"
export EVAL_TRANSPORT=dsh
export EVALUATOR_MAX_TOKENS="${EVALUATOR_MAX_TOKENS:-4096}"
# Global to every dsh node. Qwen rejects the harness default "high"; "low" is
# also what the repo already uses for DeepSeek on the http path.
export DSH_REASONING_EFFORT="${DSH_REASONING_EFFORT:-low}"

# ---- judge: local Qwen, same server (self-judging -- see env_local_judge.sh) ----
export JUDGE_ROUTE=openai
export OPENAI_BASE_URL=http://localhost:8002/v1
export OPENAI_API_KEY="$VLLM_API_KEY"
export JUDGE_MODEL=Qwen/Qwen3.8-27B
export JUDGE_TRANSPORT=http
export JUDGE_MAX_TOKENS="${JUDGE_MAX_TOKENS:-2048}"
export USE_LLM_JUDGE=true

export GATEMEM_REPO="$_REPO_ROOT"
export MOCK_MODE=false
export SKIP_FULL_STAGE=true     # 50-checkpoint slice only, always

# ---- run limits ----
# The repo defaults (6h, 20M tokens) would likely stop a 20-iteration run
# early: local Evaluator/Judge tokens count toward the same total. The
# OpenRouter key's own spend limit still caps what this can cost.
export MAX_WALLCLOCK_S="${MAX_WALLCLOCK_S:-172800}"      # 48h
export MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-100000000}" # 100M
# dsh timeouts: repo defaults (sized for DeepSeek on OpenRouter).
unset DSH_ARCHITECT_TIMEOUT_S DSH_DEFAULT_TIMEOUT_S DSH_DEVELOPER_TIMEOUT_S
