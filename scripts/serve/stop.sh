#!/usr/bin/env bash
# Stop every server these scripts started. SIGTERM first: vLLM needs a moment
# to release GPU memory, and SIGKILL leaves the allocation held until the
# kernel reaps it, which makes the next `pick_gpus` see a busy card.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

shopt -s nullglob
pids=("$RUN_DIR"/*.pid)
(( ${#pids[@]} )) || { log "nothing to stop"; exit 0; }

for f in "${pids[@]}"; do
    name="$(basename "$f" .pid)"; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
        log "stopping $name (pid $pid)"
        kill -TERM "$pid" 2>/dev/null || true
        for _ in $(seq "${STOP_GRACE_S:-45}"); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "$name ignored SIGTERM; sending SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    else
        log "$name (pid $pid) was not running"
    fi
    rm -f "$f" "${f%.pid}.gpu"
done
log "done. GPU state:"
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
