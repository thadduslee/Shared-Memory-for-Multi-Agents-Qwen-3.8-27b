#!/usr/bin/env bash
# The whole evaluation of one GateMem domain, unattended. This is the medical run's
# autopilot (runs/medical/autopilot.sh), made domain-generic. Run it inside tmux:
#
#   bash pipeline/run_domain.sh DOMAIN [--policy POLICY_DIR] [--run-dir DIR] [--rejudge] [--no-launch]
#
#   1. policy: generate one with the model (pipeline/build_policy.sh; restarted up to 2
#      times if it dies), or use --policy, a directory written by build_role_policy.py
#   2. freeze it into the run as role_policy.json (+sha256, provenance) and write
#      policy-comparison.md (pipeline/freeze_policy.py); a failed check stops here
#   3. offline smoke test with the frozen policy (pipeline/smoke_test.sh)
#   4. launch every episode (pipeline/launch.sh; SOCLAAS_EPISODES, MAX_PARALLEL, SESSION)
#   5. when every episode has an exit code, pool the scores (pipeline/compile_summary.py)
#   6. with --rejudge, also the GPT-4.1 original-judge and strict rejudges (OpenRouter)
#
# Default run directory: runs/<domain>. Everything is logged to RUN_DIR/autopilot.log.
# Nothing is launched if any check fails. --no-launch stops after step 3.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
DOMAIN=${1:?usage: run_domain.sh DOMAIN [--policy DIR] [--run-dir DIR] [--rejudge] [--no-launch]}; shift
case " medical office education household " in *" $DOMAIN "*) ;; *) die "unknown domain $DOMAIN" ;; esac
POLICY= RUN=$REPO/runs/$DOMAIN REJUDGE=0 LAUNCH=1
while [ $# -gt 0 ]; do
  case $1 in
    --policy) POLICY=$(cd "$2" && pwd); shift 2 ;;
    --run-dir) mkdir -p "$2"; RUN=$(cd "$2" && pwd); shift 2 ;;
    --rejudge) REJUDGE=1; shift ;;
    --no-launch) LAUNCH=0; shift ;;
    *) die "unknown option $1" ;;
  esac
done
mkdir -p "$RUN"; LOG=$RUN/autopilot.log
log() { echo "$(date '+%F %T %z') $*" | tee -a "$LOG"; }
stop() { log "STOPPED: $*; nothing launched"; exit 1; }
[ -n "$(ls -A "$RUN/results" 2>/dev/null)" ] && stop "$RUN/results is not empty (finished or running run); use a new --run-dir"
[ -f "$CONFIG/model.env" ] || stop "no $CONFIG/model.env (copy model.env.example)"
log "autopilot started: domain $DOMAIN, run $RUN"

# 1. policy
if [ -z "$POLICY" ]; then
  for try in 1 2 3; do
    until endpoint_up "$CONFIG/model.env"; do log "waiting for the model endpoint"; sleep 30; done
    log "generating the $DOMAIN policy (attempt $try)"
    OUT=$(bash "$REPO/pipeline/build_policy.sh" "$DOMAIN" 2>>"$LOG" | tail -1)
    if [ -n "$OUT" ] && [ -f "$OUT/policy.json" ]; then POLICY=$OUT; break; fi
    log "policy generation failed (see policies/$DOMAIN-v2-*.log)"
  done
  [ -n "$POLICY" ] || stop "policy generation failed 3 times"
fi
log "policy ready: $POLICY"

# 2. check, freeze, compare
"$PY" "$REPO/pipeline/freeze_policy.py" "$DOMAIN" "$POLICY" "$RUN" >> "$LOG" 2>&1 || stop "policy check failed (see autopilot.log)"
log "policy frozen: $(cat "$RUN/role_policy.json.sha256")"

# 3. offline smoke test
bash "$REPO/pipeline/smoke_test.sh" "$RUN" >> "$LOG" 2>&1 || stop "offline smoke test failed (smoke-test/run.log)"
log "offline smoke test passed"
[ "$LAUNCH" = 1 ] || { log "--no-launch: stopping before launch"; exit 0; }

# 4. launch
until endpoint_up "$CONFIG/model.env"; do log "waiting for the model endpoint"; sleep 30; done
bash "$REPO/pipeline/launch.sh" "$RUN" >> "$LOG" 2>&1 || stop "launch.sh refused (see autopilot.log)"
N=$(cd "$REPO/pipeline" && "$PY" -c "import common,sys; print(len(common.episode_tags(sys.argv[1])))" "$DOMAIN")
log "launched all $N episodes"

# 5. pool the scores once every episode has finished
while [ "$(ls "$RUN"/results/*/exit-code.txt 2>/dev/null | wc -l)" -lt "$N" ]; do sleep 300; done
log "all $N episodes have exit codes: $(cat "$RUN"/results/*/exit-code.txt | sort | uniq -c | tr '\n' ' ')"
"$PY" "$REPO/pipeline/compile_summary.py" "$RUN" >> "$LOG" 2>&1 && log "summary written: $RUN/summary.json" || log "compile_summary.py failed"

# 6. optional rejudges
if [ "$REJUDGE" = 1 ]; then
  bash "$REPO/pipeline/rejudge_gpt41.sh" "$RUN" >> "$LOG" 2>&1 && log "GPT-4.1 rejudge written: $RUN/gpt-4.1-rejudge" || log "GPT-4.1 rejudge failed"
  "$PY" "$REPO/pipeline/rejudge_strict.py" "$RUN" >> "$LOG" 2>&1 && log "strict rejudge written: $RUN/gpt-4.1-strict-rejudge" || log "strict rejudge failed"
fi
log "autopilot finished"
