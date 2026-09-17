#!/usr/bin/env bash
# Shared helpers for the serving scripts. Sourced, never executed.
#
# THE GPU MATH, because it is the whole reason these scripts exist.
#
#   Qwen3.8-27B in bf16 is 27e9 params x 2 bytes = ~54 GB of weights.
#   An H200 has 141 GiB (143771 MiB) of HBM3e.
#
# So the model fits on ONE H200 with ~73 GB left over for KV cache and
# activations at --gpu-memory-utilization 0.90. The repo's serve_qwen38.sh uses
# --tensor-parallel-size 2 because it was written for L40S cards, which have
# 48 GB each: 54 GB of weights does not fit on one, so the weights HAD to be
# sharded. That constraint is gone here.
#
# TP=1 is not merely sufficient, it is FASTER for this workload. Tensor
# parallelism splits every layer across GPUs and pays an all-reduce on each
# one; it buys capacity for a model that does not fit, at a latency cost. When
# the model does fit, a second GPU is better spent running a second
# independent replica -- which is what scale_evaluator.sh does -- than halving
# the latency-bound work of the first.

set -euo pipefail

VLLM_ENV="${VLLM_ENV:-/home/lweilun/miniconda3/envs/gatemem-vllm}"
VLLM_BIN="$VLLM_ENV/bin/vllm"

# REQUIRED, not cosmetic. This box's system libstdc++ predates CXXABI_1.3.15,
# which conda-forge's libicui18n needs, so the `vllm` entry point dies on
# `import sqlite3` before it prints anything useful:
#
#   ImportError: /lib/x86_64-linux-gnu/libstdc++.so.6: version `CXXABI_1.3.15'
#   not found (required by .../libicui18n.so.78)
#
# `python -c "import sqlite3"` succeeds in the same env, which makes this look
# like a vLLM bug rather than a loader-order one. Putting the env's own lib
# first fixes it for every child process the scripts spawn.
export LD_LIBRARY_PATH="$VLLM_ENV/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/.serve-logs}"
RUN_DIR="${RUN_DIR:-$REPO_ROOT/.serve-run}"
mkdir -p "$LOG_DIR" "$RUN_DIR"

# Weights live on /node2, not on /home. /home is an 879 GB disk that was at 87%
# BEFORE the first 52 GB model landed on it; /node2 is 24 TB with 6.4 TB free.
# A second model on /home would not have fitted at all.
export HF_HOME="${HF_HOME:-/node2/alfred/huggingface}"
# These two override HF_HOME when set and are a common source of a model
# silently downloading to the wrong disk -- which on /home now means a failed
# download rather than a slow one. serve_qwen38.sh unsets them; so do we.
unset HF_HUB_CACHE TRANSFORMERS_CACHE 2>/dev/null || true

[[ -d "$HF_HOME" ]] || warn "HF_HOME=$HF_HOME does not exist yet; it will be created and the model downloaded into it."

log()  { printf '\033[1;34m[serve]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[warn]\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m  %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# GPU selection
# --------------------------------------------------------------------------

# Free GPU indices, least-used first. "Free" means under $GPU_FREE_THRESHOLD_MIB
# of memory in use -- this box shares GPUs with other tenants, so hardcoding
# CUDA_VISIBLE_DEVICES (as serve_qwen38.sh does with "4,5") will eventually
# land a server on top of someone else's job.
free_gpus() {
    local threshold="${GPU_FREE_THRESHOLD_MIB:-2048}"
    nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits \
        | awk -F', ' -v t="$threshold" '$2 < t {print $2, $1}' \
        | sort -n | awk '{print $2}'
}

# GPUs already CLAIMED by a server these scripts started, whether or not it has
# allocated its memory yet.
#
# WITHOUT THIS, TWO SERVERS STARTED BACK TO BACK RACE ONTO THE SAME CARD.
# `free_gpus` reads an instantaneous nvidia-smi, and vLLM takes ~30s between
# launch and its first allocation -- so a server launched seconds ago is
# invisible and the next `pick_gpus` hands out the card it is about to take.
# The loser dies at startup with
#
#   ValueError: Free memory on device cuda:0 (86.76/139.8 GiB) on startup is
#   less than desired GPU memory utilization (0.9, 125.82 GiB)
#
# which names memory pressure and says nothing about the collision that caused
# it. Each start_server records its card in $RUN_DIR/<name>.gpu; a claim whose
# pid is gone is ignored, so a crashed server does not hold a card forever.
claimed_gpus() {
    shopt -s nullglob
    local f name pid
    for f in "$RUN_DIR"/*.gpu; do
        name="$(basename "$f" .gpu)"
        pid="$(cat "$RUN_DIR/$name.pid" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            tr ',' '\n' < "$f"
        fi
    done
}

# pick_gpus N -> comma-separated indices, or die with what was actually free.
pick_gpus() {
    local want="$1" picked
    if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
        log "using CUDA_VISIBLE_DEVICES from the environment: $CUDA_VISIBLE_DEVICES"
        echo "$CUDA_VISIBLE_DEVICES"
        return 0
    fi
    local claimed
    claimed="$(claimed_gpus | sort -u)"
    picked="$(free_gpus | { [[ -n "$claimed" ]] && grep -vxF -f <(echo "$claimed") || cat; } \
              | head -n "$want" | paste -sd, -)"
    local got
    got="$(awk -F, '{print NF}' <<<"$picked")"
    [[ -z "$picked" ]] && got=0
    (( got >= want )) || die "need $want free GPU(s), found $got (free: ${picked:-none}).
   Override with CUDA_VISIBLE_DEVICES=... or lower GPU_FREE_THRESHOLD_MIB."
    echo "$picked"
}

# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------

port_in_use() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }

# wait_for_ready PORT NAME TIMEOUT_S -- polls /v1/models until the server answers.
# A first run also downloads ~54 GB of weights, so the default timeout is long.
wait_for_ready() {
    local port="$1" name="$2" timeout="${3:-3600}" waited=0
    log "waiting for $name on :$port (timeout ${timeout}s; a first run downloads the weights)"
    while (( waited < timeout )); do
        # The Authorization header is REQUIRED even here: vLLM was started with
        # --api-key, so an unauthenticated probe gets 401 forever and the wait
        # times out against a server that is actually up and healthy.
        if curl -fsS --max-time 5 \
             -H "Authorization: Bearer ${LOCAL_API_KEY:-sk-local-dummy-key}" \
             "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
            log "$name is up on :$port"
            return 0
        fi
        # Fail fast if the child died rather than burning the whole timeout.
        if [[ -f "$RUN_DIR/$name.pid" ]] && ! kill -0 "$(cat "$RUN_DIR/$name.pid")" 2>/dev/null; then
            die "$name exited during startup. Last 40 lines of $LOG_DIR/$name.log:
$(tail -40 "$LOG_DIR/$name.log" 2>/dev/null)"
        fi
        sleep 5; waited=$((waited + 5))
    done
    die "$name did not become ready within ${timeout}s; see $LOG_DIR/$name.log"
}

# start_server NAME PORT GPUS TP -- everything else comes from the caller's
# SERVE_MODEL / SERVE_NAME / extra args in SERVE_EXTRA_ARGS.
start_server() {
    local name="$1" port="$2" gpus="$3" tp="$4"

    [[ -x "$VLLM_BIN" ]] || die "no vllm at $VLLM_BIN.
   Create it with:  scripts/serve/install_vllm.sh"

    if port_in_use "$port"; then
        warn "port $port is already in use -- assuming $name is already serving."
        warn "run scripts/serve/stop.sh first if you want a fresh server."
        return 0
    fi

    log "$name: model=$SERVE_MODEL served-as=$SERVE_NAME gpus=$gpus tp=$tp port=$port"

    CUDA_VISIBLE_DEVICES="$gpus" nohup "$VLLM_BIN" serve "$SERVE_MODEL" \
        --served-model-name "$SERVE_NAME" \
        --tensor-parallel-size "$tp" \
        --dtype bfloat16 \
        --max-model-len "${MAX_MODEL_LEN:-65536}" \
        --gpu-memory-utilization "${GPU_MEM_UTIL:-0.90}" \
        --host 0.0.0.0 \
        --port "$port" \
        --api-key "${LOCAL_API_KEY:-sk-local-dummy-key}" \
        ${SERVE_EXTRA_ARGS:-} \
        > "$LOG_DIR/$name.log" 2>&1 &

    echo $! > "$RUN_DIR/$name.pid"
    # Claim the card before anyone else can pick it; see `claimed_gpus`.
    echo "$gpus" > "$RUN_DIR/$name.gpu"
    log "$name pid $(cat "$RUN_DIR/$name.pid"), gpu $gpus, log $LOG_DIR/$name.log"
}
