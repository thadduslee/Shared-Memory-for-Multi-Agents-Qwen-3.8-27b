#!/usr/bin/env bash
# Judge any GateMem predictions.jsonl with govmem's judge (Qwen3.8-27B, thinking off), the instrument behind
# every number in scripts/a_mem_gov/README.md. Writes <judge_dir>/baselines/<name>/{scores.json,verdicts.jsonl}.
#
#   scripts/a_mem_gov/judge.sh <domain> <name> <predictions.jsonl>
#
# Judge endpoint: GOVMEM_JUDGE_BASE_URL / GOVMEM_JUDGE_API_KEY / GOVMEM_JUDGE_MODEL, falling back to
# SOCLAAS_BASE_URL / SOCLAAS_API_KEY / SOCLAAS_MODEL (.env). AMG_PYTHON needs govmem's deps.
set -u
DOMAIN=${1:?usage: judge.sh <domain> <name> <predictions.jsonl>}; NAME=${2:?}; PRED=$(realpath "${3:?}")
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
[ -f .env ] && { set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env); set +a; }
export GATEMEM_REPO="$ROOT" PYTHONPATH="$ROOT/govmem/src${PYTHONPATH:+:$PYTHONPATH}"
export GOVMEM_JUDGE_THINKING=${GOVMEM_JUDGE_THINKING:-0} GOVMEM_JUDGE_MAX_CONCURRENCY=${GOVMEM_JUDGE_MAX_CONCURRENCY:-4}
export SOCLAAS_MODEL=${SOCLAAS_MODEL:-qwen3.8:27b}
[ -n "${SSL_CERT_FILE:-}" ] && export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
J=${AMG_JUDGE_DIR:-${AMG_OUT:-outputs_amem_gov}/judge_$DOMAIN}
mkdir -p "$J"
# rescore restricts to the episodes in run.json; judge the whole domain.
[ -f "$J/run.json" ] || "${AMG_PYTHON:-python}" - "$DOMAIN" "$J/run.json" <<'E'
import json, sys
d, out = sys.argv[1], sys.argv[2]
eps = [json.loads(l)["episode_id"] for l in open(f"bench/data/{d}/episodes.jsonl")]
json.dump({"domain": d, "episodes": eps}, open(out, "w"))
E
"${AMG_PYTHON:-python}" -m govmem.eval.rescore --run-dir "$J" --name "$NAME" --predictions "$PRED"
