#!/usr/bin/env bash
# One episode of a run, with the exact harness settings of the medical run.
#
#   bash pipeline/run_episode.sh RUN_DIR BACKEND _NNN_
#
# BACKEND names an env file in pipeline/config (local -> model.env, soclaas -> soclaas.env,
# anything else -> <name>.env). The script waits until that endpoint answers, runs the
# episode into RUN_DIR/results/_NNN_/, and on a failure resumes from the episode's own
# journal, up to 5 attempts. The last exit code goes to results/_NNN_/exit-code.txt.
# With MAX_PARALLEL=N (set by launch.sh) it first waits for one of N slots.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
RUN=$(cd "${1:?usage: run_episode.sh RUN_DIR BACKEND _NNN_}" && pwd); BACKEND=${2:?}; EP=${3:?}
case $BACKEND in local) ENVF=$CONFIG/model.env ;; *) ENVF=$CONFIG/$BACKEND.env ;; esac
[ -f "$ENVF" ] || die "no $ENVF"
DOMAIN=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['domain'])" "$RUN/role_policy.json")
D=$RUN/results/$EP; umask 077; mkdir -p "$D"
export CUDA_VISIBLE_DEVICES= OMP_NUM_THREADS=4 MKL_NUM_THREADS=4

if [ "${MAX_PARALLEL:-0}" -gt 0 ]; then
  mkdir -p "$RUN/.slots"
  echo "waiting for one of $MAX_PARALLEL slots"
  while :; do
    for i in $(seq 1 "$MAX_PARALLEL"); do
      exec {slot}>"$RUN/.slots/$i.lock"
      if flock -n "$slot"; then echo "slot $i"; break 2; fi
      exec {slot}>&-
    done
    sleep 30
  done
fi

cd "$REPO"
code=1
for attempt in 1 2 3 4 5; do
  until endpoint_up "$ENVF"; do sleep 15; done
  RESUME=(); [ -f "$D/manifest.json" ] && RESUME=(--resume)
  "$PY" -u -m evaluation.gatemem_medical --domain "$DOMAIN" --env-file "$ENVF" --gatemem-repo "$DATA" \
    --permission-mode inference --permission-version 19 --answer-validation-mode enforce \
    --role-policy "$RUN/role_policy.json" --learned-access-sources 2 --reply-access \
    --episode-contains "$EP" --run-dir "$D" --answer-validation-log "$D/raw-replies.jsonl" "${RESUME[@]}" 2>&1 | tee -a "$D/run.log"
  code=${PIPESTATUS[0]}
  [ "$code" -eq 0 ] && break
  echo "attempt $attempt exited $code at $(date -u +%FT%TZ)" | tee -a "$D/run.log"
  [ "$attempt" -lt 5 ] && sleep 60
done
echo "$code" > "$D/exit-code.txt"
echo "finished $EP exit $code"
exit "$code"
