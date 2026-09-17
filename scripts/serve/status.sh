#!/usr/bin/env bash
# What is serving, on which GPU, and does it answer.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "GPUs:"
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
           --format=csv,noheader | sed 's/^/  /'
echo
echo "free for a new server (<${GPU_FREE_THRESHOLD_MIB:-2048} MiB used): $(free_gpus | paste -sd, - || echo none)"
echo
shopt -s nullglob
pids=("$RUN_DIR"/*.pid)
(( ${#pids[@]} )) || { echo "no servers started by these scripts"; exit 0; }
echo "servers:"
for f in "${pids[@]}"; do
    name="$(basename "$f" .pid)"; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
        # The port is not in the pidfile; recover it from the process's own args.
        port="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -A1 -x -- --port | tail -1)"
        model="$(curl -fsS --max-time 3 \
                 -H "Authorization: Bearer ${LOCAL_API_KEY:-sk-local-dummy-key}" \
                 "http://127.0.0.1:${port:-0}/v1/models" 2>/dev/null \
                 | "$VLLM_ENV/bin/python" -c 'import json,sys;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)"
        if [[ -n "$model" ]]; then desc="serving: $model"
        else desc="NOT ANSWERING (still loading? see $LOG_DIR/$name.log)"; fi
        printf '  %-14s pid=%-8s port=%-6s %s\n' "$name" "$pid" "${port:-?}" "$desc"
    else
        printf '  %-14s pid=%-8s DEAD (stale pidfile)\n' "$name" "$pid"
    fi
done
