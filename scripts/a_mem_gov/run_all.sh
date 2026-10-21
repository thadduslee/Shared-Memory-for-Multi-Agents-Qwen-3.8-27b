#!/usr/bin/env bash
# Run a_mem_gov on all four GateMem domains in parallel, judge each, and judge the tracked baselines.
#
#   scripts/a_mem_gov/run_all.sh [run_suffix]        e.g. run_all.sh v2a  ->  <domain>_qwen38_27b_a_mem_gov_v2a
#
# Settings come from run.sh / judge.sh (AMG_*, GOVMEM_JUDGE_*). With a dedicated local vLLM, raise
# AMG_CONCURRENCY (e.g. 24) so all episodes run at once; on SoCLaaS keep 4 domains x 3 = 12 calls in flight.
set -u
SUF=${1:+_$1}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT=${AMG_OUT:-outputs_amem_gov}
cd "$ROOT"
mkdir -p "$OUT/logs"
one() {
  local d=$1 name=${1}_qwen38_27b_a_mem_gov$SUF
  "$HERE/run.sh" "$d" "$name" > "$OUT/logs/run_$name.log" 2>&1 \
    && "$HERE/judge.sh" "$d" "a_mem_gov$SUF" "$OUT/$name/predictions.jsonl" > "$OUT/logs/judge_$name.log" 2>&1
  echo "$(date +%T) $d: $(tail -1 "$OUT/logs/judge_$name.log" 2>/dev/null)"
}
for d in medical office education household; do one "$d" & done
wait
# Baselines whose predictions are in git (outputs/): judge them with the same instrument for the comparison.
# Education/household baselines and GSM runs live in untracked gsm-rebuild/outputs on the original machine.
for d in medical office; do
  for b in a_mem long_context; do
    p=outputs/${d}_qwen38_27b_${b}/predictions.jsonl
    [ -f "$p" ] && echo "$(date +%T) $d $b: $("$HERE/judge.sh" "$d" "$b" "$p" 2>&1 | tail -1)"
  done
done
