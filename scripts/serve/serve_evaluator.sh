#!/usr/bin/env bash
# The Medical Evaluator -- the ONLY node a real run must self-host.
#
# ONE H200. The other four nodes are API-backed (Architect/Developer/Critic on
# OpenRouter, Judge on OpenAI), so this single server is the whole GPU
# requirement for `python main.py --real`.
#
#   scripts/serve/serve_evaluator.sh              # pick a free GPU, serve, wait
#   scripts/serve/serve_evaluator.sh --no-wait    # return as soon as it is launched
#
# Then point the orchestrator at it:
#
#   export VLLM_BASE_URL=http://localhost:8000/v1
#   export VLLM_API_KEY=sk-local-dummy-key
#   export EVALUATOR_MODEL=Qwen/Qwen3.8-27B
#   export VLLM_MAX_CONCURRENCY=48
#
# ==========================================================================
# WHY EVALUATOR_MODEL MUST CARRY THE "Qwen/" PREFIX
# ==========================================================================
# config.EVALUATOR_MODEL defaults to the bare slug "qwen3.8-27b", and
# config.vllm_chat_template_kwargs only emits {"enable_thinking": false} for a
# model whose name starts with one of VLLM_THINKING_MODEL_PREFIXES
# ("Qwen/", "qwen/"). The bare slug matches NEITHER, so the thinking-disable
# silently does not fire:
#
#   vllm_chat_template_kwargs("qwen3.8-27b")      -> None
#   vllm_chat_template_kwargs("Qwen/Qwen3.8-27B") -> {'enable_thinking': False}
#
# Qwen3.8 has thinking ON by default in its chat template. Left on, the
# evaluator reasons past the read timeout and records an EMPTY answer for
# every checkpoint that retrieved successfully -- which reads downstream as a
# total retrieval failure, not as a timeout. That is the `render_degraded`
# failure the Auto Research README describes costing a 3-hour, 12.5M-token run.
#
# So this script serves the model UNDER ITS PREFIXED NAME and the exported
# EVALUATOR_MODEL must match it exactly. `--reasoning-parser qwen3` below is
# the second, independent guard: it splits any reasoning that IS generated into
# `reasoning_content`, leaving `content` as the answer alone. Belt and braces,
# because each one alone has failed in this repo's history.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

NAME="evaluator"
PORT="${EVALUATOR_PORT:-8000}"
SERVE_MODEL="${EVALUATOR_HF_MODEL:-Qwen/Qwen3.8-27B}"
# The served name IS the contract with config.EVALUATOR_MODEL. Keep the prefix.
SERVE_NAME="${EVALUATOR_SERVED_NAME:-$SERVE_MODEL}"
# --enable-auto-tool-choice is REQUIRED even though the Evaluator has no tools
# of its own. On EVAL_TRANSPORT=dsh the harness mounts its own Cordis tool
# plugins and therefore sends `tool_choice: "auto"` on EVERY request; a server
# without a tool parser rejects that outright:
#
#   "auto" tool choice requires --enable-auto-tool-choice and
#   --tool-call-parser to be set   [400 INVALID_REQUEST]
#
# Every answering checkpoint then falls back to raw evidence and the run halts
# with ANSWERER DEGRADED -- correctly, but for a reason the message attributes
# to an unreachable endpoint rather than to a rejected request.
SERVE_EXTRA_ARGS="--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser ${EVALUATOR_TOOL_PARSER:-qwen3_coder} --limit-mm-per-prompt {\"image\":0,\"video\":0}"

GPUS="$(pick_gpus "${EVALUATOR_TP:-1}")"
start_server "$NAME" "$PORT" "$GPUS" "${EVALUATOR_TP:-1}"

if [[ "${1:-}" != "--no-wait" ]]; then
    wait_for_ready "$PORT" "$NAME" "${STARTUP_TIMEOUT_S:-3600}"
    cat <<EOF

  Evaluator ready.  Export these before \`python main.py --real\`:

    export VLLM_BASE_URL=http://localhost:$PORT/v1
    export VLLM_API_KEY=${LOCAL_API_KEY:-sk-local-dummy-key}
    export EVALUATOR_MODEL=$SERVE_NAME
    export VLLM_MAX_CONCURRENCY=${SUGGESTED_CONCURRENCY:-48}

  Verify the reasoning split before starting a long run:
    scripts/serve/healthcheck.sh

EOF
fi
