#!/usr/bin/env bash
# Generate a domain's role policy with the system's own model (scripts/build_role_policy.py).
#
#   bash pipeline/build_policy.sh DOMAIN            # writes policies/<domain>-v2-<UTC time>/
#
# Uses the model endpoint in pipeline/config/model.env (AMEM_LLM_BASE_URL/MODEL/API_KEY).
# Inputs are the job titles in the data and the benchmark's published policy paragraph
# only; excluded roles and whether authorities are voted on come from hosts.py and the
# builder's DOMAINS table. Prints the output directory on the last line.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
DOMAIN=${1:?usage: build_policy.sh DOMAIN}
ENVF=${MODEL_ENV:-$CONFIG/model.env}
[ -f "$ENVF" ] || die "no $ENVF (copy pipeline/config/model.env.example)"
endpoint_up "$ENVF" || die "model endpoint in $ENVF is not answering"
OUT=$REPO/policies/$DOMAIN-v2-$(date -u +%Y%m%dT%H%M%SZ)
cd "$REPO"
"$PY" -u scripts/build_role_policy.py --domain "$DOMAIN" --gatemem-repo "$DATA" \
  --base-url "$(env_value "$ENVF" AMEM_LLM_BASE_URL)" --model "$(env_value "$ENVF" AMEM_LLM_MODEL)" \
  --api-key "$(env_value "$ENVF" AMEM_LLM_API_KEY)" --out "$OUT" "${@:2}" 2>&1 | tee "$OUT.log"
[ "${PIPESTATUS[0]}" -eq 0 ] && [ -f "$OUT/policy.json" ] || die "policy generation failed (see $OUT.log)"
echo "$OUT"
