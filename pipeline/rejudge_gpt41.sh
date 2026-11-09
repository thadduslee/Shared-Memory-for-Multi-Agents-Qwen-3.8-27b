#!/usr/bin/env bash
# GPT-4.1 rejudge with the benchmark's original judge (the harness scorer), via OpenRouter.
#
#   bash pipeline/rejudge_gpt41.sh runs/<domain>            # start, or resume if already started
#   bash pipeline/rejudge_gpt41.sh runs/<domain> --preflight   # one tiny request to check the key
#
# 1. pipeline/combine_predictions.py merges the episodes into <run>/combined-predictions
# 2. python -m evaluation.gatemem_medical.rejudge scores every checkpoint into
#    <run>/gpt-4.1-rejudge (rejudge.sqlite journal, scores.jsonl, summary.json, manifest.json)
# Needs AMEM_OPENROUTER_API_KEY in pipeline/config/openrouter.env. No GPU or local model.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
RUN=$(cd "${1:?usage: rejudge_gpt41.sh RUN_DIR [--preflight]}" && pwd); shift
ENVF=${OPENROUTER_ENV:-$REPO/pipeline/config/openrouter.env}
[ -f "$ENVF" ] || die "no $ENVF (copy pipeline/config/openrouter.env.example and add the key)"
cd "$REPO"
if [ "${1:-}" = --preflight ]; then
  exec "$PY" -m evaluation.gatemem_medical.rejudge --env-file "$ENVF" --source-run "$RUN" --preflight
fi
"$PY" pipeline/combine_predictions.py "$RUN"
DOMAIN=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['domain'])" "$RUN/combined-predictions/manifest.json")
OUT=$RUN/gpt-4.1-rejudge
RESUME=(); [ -f "$OUT/rejudge.sqlite" ] && RESUME=(--resume)
"$PY" -u -m evaluation.gatemem_medical.rejudge --env-file "$ENVF" --domain "$DOMAIN" \
  --gatemem-repo "$DATA" --source-run "$RUN/combined-predictions" --output-dir "$OUT" "${RESUME[@]}" "$@" \
  2>&1 | tee -a "$OUT.log"
