#!/usr/bin/env bash
# OPTIONAL second node: a local Judge, so the loop needs no OpenAI key.
#
# You do NOT need this for a normal run -- JUDGE_ROUTE=openai with an
# OPENAI_API_KEY costs one GPU less and is the shipped default. Serve a judge
# locally when you want to run the loop fully offline, or to re-score archived
# predictions (rescore_run.py) without paying per checkpoint.
#
#   scripts/serve/serve_judge.sh                  # second free GPU, port 8001
#
# Then:
#   export JUDGE_ROUTE=openai
#   export OPENAI_BASE_URL=http://localhost:8001/v1
#   export OPENAI_API_KEY=sk-local-dummy-key
#   export JUDGE_MODEL=Qwen/Qwen3-32B
#
# ==========================================================================
# READ THIS BEFORE USING A LOCAL JUDGE FOR A HEADLINE NUMBER
# ==========================================================================
# The Judge decides `utility_ok`, and under JUDGE_IMPL=gatemem it is PRIMARY on
# utility -- it can overturn the rule matcher. That authority is what makes
# judge quality load-bearing rather than cosmetic.
#
# TWO HAZARDS, both real:
#
# 1. SELF-JUDGING. If JUDGE_MODEL is the same model as EVALUATOR_MODEL, the
#    system is grading its own homework, and a shared blind spot (the same
#    misreading of a question, the same paraphrase habit) becomes an inflated
#    score rather than a caught error. This script therefore defaults to a
#    DIFFERENT model from the evaluator and refuses to start if you point it
#    at the same one without JUDGE_ALLOW_SELF_JUDGE=1.
#
# 2. COMPARABILITY. GateMem's published numbers use gpt-4.1. A local judge
#    produces numbers that are internally consistent -- fine for "did this
#    iteration beat the last one" -- but NOT directly comparable to the paper,
#    the leaderboard, or outputs/. The repo already has an `outputs_qwenjudge_backup/`
#    tree precisely because a Qwen judge and a GPT-4.1 judge disagree enough to
#    be worth keeping apart.
#
# Use a local judge to steer the loop. Re-judge with gpt-4.1 before reporting.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

NAME="judge"
PORT="${JUDGE_PORT:-8001}"
SERVE_MODEL="${JUDGE_HF_MODEL:-Qwen/Qwen3-32B}"
SERVE_NAME="${JUDGE_SERVED_NAME:-$SERVE_MODEL}"
# A judge emits one short JSON verdict; it needs none of the evaluator's
# context length, and a smaller window leaves far more room for KV cache.
MAX_MODEL_LEN="${JUDGE_MAX_MODEL_LEN:-16384}"
SERVE_EXTRA_ARGS="--reasoning-parser qwen3"

EVAL_NAME="${EVALUATOR_SERVED_NAME:-${EVALUATOR_HF_MODEL:-Qwen/Qwen3.8-27B}}"
if [[ "$SERVE_NAME" == "$EVAL_NAME" && "${JUDGE_ALLOW_SELF_JUDGE:-0}" != "1" ]]; then
    die "JUDGE_MODEL == EVALUATOR_MODEL ($SERVE_NAME): the system would grade itself.
   Pick a different judge, or set JUDGE_ALLOW_SELF_JUDGE=1 if that is genuinely
   what you want (it is not, for any number you intend to report)."
fi

GPUS="$(pick_gpus "${JUDGE_TP:-1}")"
start_server "$NAME" "$PORT" "$GPUS" "${JUDGE_TP:-1}"

if [[ "${1:-}" != "--no-wait" ]]; then
    wait_for_ready "$PORT" "$NAME" "${STARTUP_TIMEOUT_S:-3600}"
    cat <<EOF

  Judge ready.  Export these to route the Judge here instead of OpenAI:

    export JUDGE_ROUTE=openai
    export OPENAI_BASE_URL=http://localhost:$PORT/v1
    export OPENAI_API_KEY=${LOCAL_API_KEY:-sk-local-dummy-key}
    export JUDGE_MODEL=$SERVE_NAME

  Reminder: numbers from a local judge are not comparable to GateMem's
  published gpt-4.1 results. Re-judge before reporting.

EOF
fi
