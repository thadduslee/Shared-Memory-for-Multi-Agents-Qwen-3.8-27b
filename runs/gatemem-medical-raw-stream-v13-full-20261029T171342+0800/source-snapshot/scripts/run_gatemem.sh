#!/usr/bin/env bash
# Full NEW standalone A-MEM evaluation. No old online-system entry point is used.
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
python_bin=${AMEM_PYTHON:-/home/thaddus/gatemem/.venv-nb/bin/python}
[[ -x "$python_bin" ]] || { echo 'Set AMEM_PYTHON to a Python with sentence-transformers installed.' >&2; exit 1; }
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
"$python_bin" -u -m evaluation.gatemem_medical --preflight
args=("$@")
has_directory=false
for arg in "$@"; do
  if [[ $arg == --run-dir || $arg == --run-dir=* ]]; then has_directory=true; fi
done
if [[ $has_directory == false ]]; then
  for arg in "$@"; do
    if [[ $arg == --resume ]]; then echo '--resume requires an explicit --run-dir.' >&2; exit 2; fi
  done
  directory="$root/runs/gatemem-medical-$(date -u +%Y%m%dT%H%M%SZ)"
  args+=(--run-dir "$directory")
  printf 'Standalone A-MEM results: %s\n' "$directory"
fi
mkdir -p "$root/runs"
log="$root/runs/evaluation-$(date -u +%Y%m%dT%H%M%SZ).log"
printf 'Evaluation log: %s\n' "$log"
"$python_bin" -u -m evaluation.gatemem_medical "${args[@]}" 2>&1 | tee "$log"
