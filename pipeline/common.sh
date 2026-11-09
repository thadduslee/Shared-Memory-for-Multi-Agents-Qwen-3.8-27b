# Sourced by the pipeline scripts. Paths are derived from this file's location, so the
# repository works wherever it is cloned.
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DATA=$REPO/data/gatemem
CONFIG=$REPO/pipeline/config
# Python: $AMEM_PYTHON, else the repository's .venv, else python3 on PATH.
if [ -n "${AMEM_PYTHON:-}" ]; then PY=$AMEM_PYTHON
elif [ -x "$REPO/.venv/bin/python" ]; then PY=$REPO/.venv/bin/python
else PY=$(command -v python3); fi
export PYTHONDONTWRITEBYTECODE=1

die() { echo "error: $*" >&2; exit 1; }

# env_value FILE KEY -> the value of KEY in an env file (empty if absent).
env_value() { sed -n "s/^$2=//p" "$1" | tail -1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }

# endpoint_up ENV_FILE -> success when that env file's AMEM_LLM_BASE_URL answers /models.
# An empty key sends no Authorization header (keyless proxies such as SOCLAAS reject "Bearer ").
endpoint_up() {
  local base key
  base=$(env_value "$1" AMEM_LLM_BASE_URL); key=$(env_value "$1" AMEM_LLM_API_KEY)
  [ -n "$base" ] || return 1
  if [ -n "$key" ]; then curl -sf -m 5 -H "Authorization: Bearer $key" "${base%/}/models" >/dev/null
  else curl -sf -m 5 "${base%/}/models" >/dev/null; fi
}
