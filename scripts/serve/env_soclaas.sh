#!/usr/bin/env bash
# Hybrid run with NO local GPU: DeepSeek agents on OpenRouter, Evaluator and
# Judge on the SoCLaaS-hosted Qwen3.8-27B (NUS). Sourced, never executed.
#
#   set -a; source .env; set +a          # OPENROUTER_API_KEY + SOCLAAS_*
#   source scripts/serve/env_soclaas.sh
#   cd "Auto Research" && python main.py --real ...
#
# This replaces env_hybrid.sh when the GPU 1 vLLM server is down. Nothing here
# needs a GPU, so there is no serve_*.sh to run first.

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set -a; [[ -f "$_REPO_ROOT/.env" ]] && source "$_REPO_ROOT/.env"; set +a
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY missing (expected in $_REPO_ROOT/.env)}"
: "${SOCLAAS_API_KEY:?SOCLAAS_API_KEY missing (expected in $_REPO_ROOT/.env)}"
: "${SOCLAAS_BASE_URL:?SOCLAAS_BASE_URL missing (expected in $_REPO_ROOT/.env)}"

# ---- agentic nodes: repo defaults, stated explicitly ----
unset AGENT_BASE_URL AGENT_API_KEY_ENV AGENT_ROUTE AGENT_IS_VLLM DSH_BASE_URL DEEPSEEK_BASE_URL DEEPSEEK_API_KEY
unset ARCHITECT_MAX_TOKENS DEVELOPER_MAX_TOKENS
export ARCHITECT_MODEL=deepseek/deepseek-v4-flash-0731
export DEVELOPER_MODEL=deepseek/deepseek-v4-flash-0731
export CRITIC_MODEL=deepseek/deepseek-v4-flash-0731
export AGENT_TRANSPORT=dsh
export DEVELOPER_TRANSPORT=http
export OPENROUTER_REASONING_EFFORT="${OPENROUTER_REASONING_EFFORT:-low}"
# Runaway guard, paired with DSH_CRITIC_TIMEOUT_S=1800 in config.py. See the
# note in env_hybrid.sh: uncapped, the Critic times out every iteration and the
# Architect never receives a critique.
export CRITIC_MAX_TOKENS="${CRITIC_MAX_TOKENS:-32768}"

# ---- evaluator + judge: SoCLaaS-hosted Qwen3.8-27B ----
# Reusing the `vllm` route rather than inventing one: SoCLaaS serves an
# OpenAI-compatible vLLM, and the route's is_vllm=True is what makes the client
# send chat_template_kwargs at all.
export VLLM_BASE_URL="$SOCLAAS_BASE_URL"
export VLLM_API_KEY="$SOCLAAS_API_KEY"
export EVALUATOR_MODEL="${SOCLAAS_MODEL:-qwen3.8:27b}"

# THE BARE SLUG BITES HERE, and this line is the whole defence.
#
# config.vllm_chat_template_kwargs only emits {"enable_thinking": false} for a
# model matching VLLM_THINKING_MODEL_PREFIXES, which defaults to "Qwen/,qwen/".
# SoCLaaS serves this model as `qwen3.8:27b` -- no vendor prefix -- so the
# default matches NOTHING and the thinking-disable silently does not fire.
# Measured on the live endpoint: "What is 17*23?" costs 65 completion tokens
# with thinking on and 4 with it off. Left on across 579 checkpoints the
# Evaluator reasons past its read timeout and records an EMPTY answer, which
# reads downstream as total retrieval failure rather than as a timeout -- the
# `render_degraded` failure the Auto Research README costs at 3 hours and
# 12.5M tokens. Adding the bare prefix makes the match fire.
export VLLM_THINKING_MODEL_PREFIXES="Qwen/,qwen/,qwen3"

# MEASURED against the live endpoint, not guessed. A burst of 8 concurrent
# requests is served cleanly; 24 returns 4x HTTP 429 and 48 returns 15x. The
# client does retry 429 with jitter (RETRYABLE_STATUS in llm/client.py), but a
# retry storm across a 579-checkpoint fan-out wastes wall clock to no purpose.
# 12 + 4 was NOT enough: a burst test measures a CONCURRENCY ceiling, but this
# endpoint also limits by request RATE, which concurrency does not bound when
# each call is fast. At 12 + 4 a 579-checkpoint pass still drew 84 429s and
# degraded 10 answering checkpoints to raw evidence. 6 + 2 halves the rate the
# Evaluator and the Judge together ask this one shared endpoint for.
export VLLM_MAX_CONCURRENCY="${VLLM_MAX_CONCURRENCY:-6}"
# 2, not 4. SoCLaaS turned out to limit by REQUEST RATE, not by concurrency:
# the first 579-checkpoint pass drew 734 HTTP 429s with this at 4, because the
# judge fans out one call per checkpoint and concurrency does not bound the
# rate a fast endpoint is asked at. Halving it halves the request rate. The
# retries in _dispatch_judge are the correctness guard; this is the politeness
# one, and it costs judging wall clock rather than accuracy.
export JUDGE_MAX_CONCURRENCY="${JUDGE_MAX_CONCURRENCY:-2}"

# RETRY LADDER, widened for a rate-limited shared endpoint.
#
# The defaults (4 attempts, base 0.75s, full jitter) all fire inside one
# rate-limit window -- an entire ladder can be spent in under a second, which
# against a RATE limit is one request sent four times. The first 579-checkpoint
# pass drew 84 HTTP 429s and burned 14 ladders to exhaustion that way.
#
# 8 attempts with a 2s base and a 60s ceiling spans minutes rather than seconds,
# so a later attempt lands in a later window. This is the transport half; the
# correctness half is EVAL_RENDER_ATTEMPTS / JUDGE_LLM_ATTEMPTS in config.py,
# which retry the WHOLE call with a guaranteed floor between tries after the
# ladder is spent.
export HTTP_MAX_RETRIES="${HTTP_MAX_RETRIES:-8}"
export HTTP_BACKOFF_BASE_S="${HTTP_BACKOFF_BASE_S:-2.0}"
export HTTP_BACKOFF_MAX_S="${HTTP_BACKOFF_MAX_S:-60.0}"

export EVAL_TRANSPORT=http
export EVALUATOR_MAX_TOKENS="${EVALUATOR_MAX_TOKENS:-4096}"
export DSH_REASONING_EFFORT="${DSH_REASONING_EFFORT:-low}"

# ---- judge: same endpoint, over the openai route ----
export JUDGE_ROUTE=openai
export OPENAI_BASE_URL="$SOCLAAS_BASE_URL"
export OPENAI_API_KEY="$SOCLAAS_API_KEY"
export JUDGE_MODEL="${SOCLAAS_MODEL:-qwen3.8:27b}"
export JUDGE_TRANSPORT=http
export JUDGE_MAX_TOKENS="${JUDGE_MAX_TOKENS:-2048}"
export USE_LLM_JUDGE=true
# The openai route infers is_vllm from its base URL and would guess wrong for a
# university hostname, leaving the Judge thinking while the Evaluator does not.
export OPENAI_ROUTE_IS_VLLM=true

# ---- benchmark scope ----
export GATEMEM_REPO="$_REPO_ROOT"
export MOCK_MODE=false
# The dev slice IS the full benchmark for this run (579 checkpoints), so the
# separate full stage would only re-evaluate the same set: --no-full stays on
# the command line and costs nothing.
export SKIP_FULL_STAGE=true
export EXPECTED_DEV_CHECKPOINTS=579

# ---- limits ----
export MAX_WALLCLOCK_S="${MAX_WALLCLOCK_S:-172800}"
export MAX_TOTAL_TOKENS="${MAX_TOTAL_TOKENS:-100000000}"

unset _REPO_ROOT
