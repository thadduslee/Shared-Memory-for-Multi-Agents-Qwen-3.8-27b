#!/usr/bin/env bash
# Three endpoints, one per job. Sourced, never executed.
#
#   Architect, Developer   deepseek-v4-flash-0731  OpenRouter
#   Critic, Judge          Qwen3.8-27B             LOCAL vLLM, GPU 6, :8002
#   Evaluator              qwen3.8:27b             SoCLaaS (NUS)
#
#   CUDA_VISIBLE_DEVICES=6 scripts/serve/serve_agents.sh
#   set -a; source .env; set +a
#   source scripts/serve/env_split.sh
#   cd "Auto Research" && python main.py --real --no-full --dev-checkpoints 579 --max-iterations 20
#
# WHY THE EVALUATOR STAYS ON SoCLaaS while the Judge moves off it: the Evaluator
# is the 579-way fan-out and wants the throughput of a machine nobody else is
# using; the Critic and Judge are the two nodes whose quality the loop's memory
# depends on, and the local server has no rate limit to degrade them.

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$_REPO_ROOT/scripts/serve/env_soclaas.sh"

# ---- Critic and Judge: local vLLM on GPU 6, over the `openai` route ----
#
# BOTH ON ONE ROUTE, and the route is what decides the endpoint. `route_for`
# hands the route NAME to the client, which looks the base URL up from its own
# table -- a profile's `base_url` is consulted only on the dsh path. So moving a
# node to a different server means giving it a route whose base URL points
# there, and `openai` is the one already free for that.
#
# `agent` would have been the obvious choice and is the wrong one: its base URL
# is AGENT_BASE_URL, which is ALSO what the Architect and Developer hand to the
# harness on dsh. Repointing it would silently move all three.
export LOCAL_VLLM_URL="${LOCAL_VLLM_URL:-http://localhost:8002/v1}"
export OPENAI_BASE_URL="$LOCAL_VLLM_URL"
export OPENAI_API_KEY="${LOCAL_API_KEY:-sk-local-dummy-key}"
# The served name must keep the "Qwen/" prefix: it is what makes
# vllm_chat_template_kwargs emit {"enable_thinking": false}. serve_agents.sh
# serves it under exactly this name.
export JUDGE_MODEL=Qwen/Qwen3.8-27B
export JUDGE_ROUTE=openai
export JUDGE_TRANSPORT=http
export OPENAI_ROUTE_IS_VLLM=true

export CRITIC_MODEL=Qwen/Qwen3.8-27B
export CRITIC_ROUTE=openai
export CRITIC_BASE_URL="$LOCAL_VLLM_URL"
export CRITIC_API_KEY_ENV=OPENAI_API_KEY
# http, not dsh: the pinned SDK (0.1.1rc1) always sends reasoningEffort=high,
# which a vLLM-served Qwen rejects with a 400 that arrives as an empty
# completion -- the same reason the Evaluator is on http. The cost is the
# Critic's fs_read, which is only real on dsh.
export CRITIC_TRANSPORT=http

# The Critic and Judge share this route's semaphore. It is a local, dedicated
# GPU with no rate limit, so this is sized for the Judge's 579-call fan-out
# rather than for politeness -- unlike the SoCLaaS numbers inherited above,
# which still govern the Evaluator.
export JUDGE_MAX_CONCURRENCY="${JUDGE_MAX_CONCURRENCY_LOCAL:-16}"

# 65536 was the value the local Qwen Critic needed: it returned EMPTY replies at
# 16384, having spent the budget before reaching its json block. 32768 was a
# guard against a runaway DeepSeek Critic on a metered API; neither concern
# applies to a dedicated GPU, and truncation is the worse failure of the two.
# Unconditional: env_soclaas.sh above already exported 32768, so a
# ${CRITIC_MAX_TOKENS:-...} default would silently keep that value.
export CRITIC_MAX_TOKENS=65536

unset _REPO_ROOT
