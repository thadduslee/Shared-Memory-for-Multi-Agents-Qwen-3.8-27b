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
unset ARCHITECT_MAX_TOKENS DEVELOPER_MAX_TOKENS
export ARCHITECT_MODEL=deepseek/deepseek-v4-flash-0731
export DEVELOPER_MODEL=deepseek/deepseek-v4-flash-0731
export CRITIC_MODEL=deepseek/deepseek-v4-flash-0731
export AGENT_TRANSPORT=dsh
export DEVELOPER_TRANSPORT=http
export OPENROUTER_REASONING_EFFORT="${OPENROUTER_REASONING_EFFORT:-low}"
# The Critic is CAPPED while the other two stay uncapped, because it is the one
# node that ran away. Left uncapped it inherits the harness default of 256000
# and, on the pinned SDK, reasoningEffort=high with it; in runs_hybrid_v2 that
# produced one still-streaming completion per iteration that hit the wall at
# 600s with tokens=0, four iterations in a row, and the run never moved off
# MGS=0.6111 because the Architect never received a critique.
#
# 32768, not 16384: a cap that truncates the reply mid-JSON is its own failure
# (a 16384-capped Critic returned empty replies earlier in this project), and a
# real critique -- prose plus a json block of mechanisms and proposals -- has
# been observed at ~31k tokens upstream. This is a runaway guard, not a budget
# the node is meant to spend. DSH_CRITIC_TIMEOUT_S (1800s, config.py) is the
# other half: neither the cap nor the wall should be the only thing in the way.
export CRITIC_MAX_TOKENS="${CRITIC_MAX_TOKENS:-32768}"

# ---- evaluator: local Qwen on the GPU 1 server ----
export VLLM_API_KEY="${VLLM_API_KEY:-sk-local-dummy-key}"
export VLLM_BASE_URL=http://localhost:8002/v1
export EVALUATOR_MODEL=Qwen/Qwen3.8-27B      # prefix required for the thinking-disable
export VLLM_MAX_CONCURRENCY="${VLLM_MAX_CONCURRENCY:-48}"
# http, NOT dsh, and this is forced by the pinned SDK rather than preferred.
#
# deepseek-harness-sdk 0.1.1rc1 (pinned -- see requirements.txt) has no
# `reasoning_effort` field, so the runtime always sends reasoningEffort=high.
# DeepSeek's endpoint accepts that; our vLLM Qwen3.8-27B answers
#
#   Unexpected reasoning effort high. Supported types are xhigh (default),
#   medium, and low.   [400 INVALID_REQUEST]
#
# which reaches the loop as an EMPTY completion, i.e. as a total retrieval
# failure rather than as a rejected request. Setting the composition's
# `defaults.reasoningEffort` does not help: the runtime ignores that key.
#
# Nothing is lost here. The Evaluator is a single-shot scorer with NO tools, so
# the harness buys it nothing, and config.py already notes that eval/judge on
# http is a supported topology. The http path also sends
# enable_thinking=false for a "Qwen/"-prefixed model, which dsh never did.
# The three AGENTIC nodes stay on dsh, where the pinned SDK's cordis
# composition is what restores least privilege.
export EVAL_TRANSPORT=http
export EVALUATOR_MAX_TOKENS="${EVALUATOR_MAX_TOKENS:-4096}"
# Harmless on 0.1.1rc1 (no such field; the adapter drops it), kept so the file
# still does the right thing if the pin is ever moved forward.
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
