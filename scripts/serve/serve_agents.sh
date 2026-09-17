#!/usr/bin/env bash
# Architect / Developer / Critic on a local Qwen3.8-27B. One H200, port 8002.
#
#   scripts/serve/serve_agents.sh
#
# Then:
#   export AGENT_BASE_URL=http://localhost:8002/v1
#   export AGENT_API_KEY_ENV=VLLM_API_KEY
#   export AGENT_ROUTE=agent
#   export ARCHITECT_MODEL=Qwen/Qwen3.8-27B
#   export DEVELOPER_MODEL=Qwen/Qwen3.8-27B
#   export CRITIC_MODEL=Qwen/Qwen3.8-27B
#
# (scripts/serve/env_local_agents.sh writes all of that for you.)
#
# ==========================================================================
# WHY A SECOND SERVER RATHER THAN REUSING THE EVALUATOR'S
# ==========================================================================
# Same model, same weights, no extra download -- so the only cost is one GPU,
# and the reason to pay it is QUEUEING, not capability.
#
# The Evaluator fans out across the whole checkpoint slice under a semaphore
# sized for that fan-out (VLLM_MAX_CONCURRENCY=48). The Architect and Critic
# issue ONE long call each per iteration, and the Developer holds a
# conversation across dozens of turns. Sharing a server would put a single
# Architect turn behind up to 48 queued evaluator requests at exactly the
# moment the iteration cannot proceed without it -- and DSH_ARCHITECT_TIMEOUT_S
# is the thing that would then fire, reported as a transport failure that
# re-runs the whole iteration.
#
# Separate servers also mean separate `max-model-len` and separate tool-calling
# config, both of which genuinely differ between the two jobs.
#
# ==========================================================================
# TOOL CALLING IS NOT OPTIONAL HERE
# ==========================================================================
# --enable-auto-tool-choice --tool-call-parser qwen3_coder is what makes the
# Developer possible at all. The Developer is handed a real OpenAI tool schema
# (nodes/dev_tools.py TOOL_SCHEMAS) and reads `tool_calls` off the reply.
# Without a tool parser vLLM returns the model's tool syntax as ordinary
# `content`, and the Auto Research README documents exactly what that costs:
# run-280411c75b99 emitted 27 such blocks, parsed zero, wrote zero bytes, and
# evaluated byte-identical code in all four iterations.
#
# `qwen3_coder` and `qwen3_xml` both resolve to Qwen3EngineToolParser in
# vLLM 0.29; qwen3_coder is the name the other tenant on this box uses for the
# same model.
#
# --max-model-len is larger than the evaluator's because the Developer's
# conversation accumulates tool output over dozens of turns, and the Architect
# inlines up to ARCHITECT_CODE_VIEW_MAX_CHARS (60k chars) of the codebase into
# a single task string.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

NAME="agents"
PORT="${AGENT_PORT:-8002}"
SERVE_MODEL="${AGENT_HF_MODEL:-Qwen/Qwen3.8-27B}"
SERVE_NAME="${AGENT_SERVED_NAME:-$SERVE_MODEL}"
MAX_MODEL_LEN="${AGENT_MAX_MODEL_LEN:-131072}"
SERVE_EXTRA_ARGS="--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser ${AGENT_TOOL_PARSER:-qwen3_coder}"

GPUS="$(pick_gpus "${AGENT_TP:-1}")"
start_server "$NAME" "$PORT" "$GPUS" "${AGENT_TP:-1}"

if [[ "${1:-}" != "--no-wait" ]]; then
    wait_for_ready "$PORT" "$NAME" "${STARTUP_TIMEOUT_S:-3600}"
    cat <<EOF

  Agent server ready on :$PORT (Architect / Developer / Critic).

    source scripts/serve/env_local_agents.sh

  Verify tool calling before a real run -- it is the one thing that fails
  silently rather than loudly:
    scripts/serve/healthcheck.sh $PORT --tools

EOF
fi
