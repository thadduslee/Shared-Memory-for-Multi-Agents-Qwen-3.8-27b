#!/usr/bin/env bash
# Add evaluator REPLICAS, one GPU each, behind one round-robin port.
#
#   scripts/serve/scale_evaluator.sh 3     # three 1-GPU replicas + a balancer
#
# Use this only when the evaluator is measurably the bottleneck. One H200
# replica already serves the whole 579-checkpoint stage; more replicas buy
# throughput, not capability, and every GPU here is one that is not running
# something else.
#
# WHY REPLICAS RATHER THAN A BIGGER --tensor-parallel-size.
# TP shards each layer across GPUs and pays an all-reduce per layer. It exists
# so a model too large for one card can run at all. Qwen3.8-27B fits on one
# H200 with ~73 GB to spare, so TP=2 here would halve per-request latency at
# best while halving the number of independent requests in flight -- a bad
# trade for a fan-out workload that is throughput-bound, not latency-bound.
# N independent replicas give N x the concurrent capacity with no collective
# communication at all.
#
# VLLM_MAX_CONCURRENCY must be raised to match, or the orchestrator's
# process-wide semaphore will keep the extra replicas idle. It is sized to the
# CLUSTER, not to the checkpoint count -- see config.py.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

N="${1:-2}"
BASE_PORT="${EVALUATOR_PORT:-8000}"
SERVE_MODEL="${EVALUATOR_HF_MODEL:-Qwen/Qwen3.8-27B}"
SERVE_NAME="${EVALUATOR_SERVED_NAME:-$SERVE_MODEL}"
SERVE_EXTRA_ARGS="--reasoning-parser qwen3 --limit-mm-per-prompt {\"image\":0,\"video\":0}"

[[ "$N" =~ ^[0-9]+$ ]] || die "usage: scale_evaluator.sh <n-replicas>"

mapfile -t GPU_LIST < <(free_gpus)
(( ${#GPU_LIST[@]} >= N )) || die "asked for $N replicas, only ${#GPU_LIST[@]} free GPU(s)"

PORTS=()
for (( i = 0; i < N; i++ )); do
    port=$((BASE_PORT + i))
    start_server "evaluator-$i" "$port" "${GPU_LIST[$i]}" 1
    PORTS+=("$port")
done

for (( i = 0; i < N; i++ )); do
    wait_for_ready "$((BASE_PORT + i))" "evaluator-$i" "${STARTUP_TIMEOUT_S:-3600}"
done

cat <<EOF

  $N evaluator replicas up on ports: ${PORTS[*]}

  vLLM has no built-in balancer. Either point the orchestrator at one replica,
  or put nginx/haproxy in front of all $N and point VLLM_BASE_URL at that.

    export VLLM_BASE_URL=http://localhost:$BASE_PORT/v1
    export EVALUATOR_MODEL=$SERVE_NAME
    export VLLM_MAX_CONCURRENCY=$((N * 48))   # raise it, or the extras idle

EOF
