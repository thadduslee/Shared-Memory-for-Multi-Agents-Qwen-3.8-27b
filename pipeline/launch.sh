#!/usr/bin/env bash
# Start every episode of a run, one tmux window each, in tmux session $SESSION.
#
#   bash pipeline/launch.sh runs/<domain>
#
# Environment:
#   SESSION           tmux session (default epm-<domain>; created if missing)
#   SOCLAAS_EPISODES  episodes to send to the SOCLAAS endpoint, e.g. "_001_ _002_" (default none);
#                     the rest use the local vLLM in pipeline/config/model.env
#   MAX_PARALLEL      at most this many episodes run at once (default 0 = all at once,
#                     which is how the medical run ran its 21 episodes)
# Refuses to start when the run was already launched (backends.json) or results/ has episodes,
# the policy changed, or the local endpoint is down. Each window gets this script's Python
# (AMEM_PYTHON), because tmux windows inherit the tmux server's environment, not the caller's.
# Writes RUN_DIR/backends.json (episode -> backend) for compile_summary.py.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
RUN=$(cd "${1:?usage: launch.sh RUN_DIR}" && pwd)
[ -f "$RUN/role_policy.json" ] || die "no role_policy.json; not launching"
(cd "$RUN" && sha256sum -c --quiet role_policy.json.sha256) || die "role_policy.json changed; not launching"
[ -f "$CONFIG/model.env" ] || die "no $CONFIG/model.env; not launching"
endpoint_up "$CONFIG/model.env" || die "local endpoint in model.env is not up; not launching"
[ -n "$(ls -A "$RUN/results" 2>/dev/null)" ] && die "$RUN/results is not empty; not launching"
[ -f "$RUN/backends.json" ] && die "$RUN was already launched (backends.json exists); not launching twice"
DOMAIN=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['domain'])" "$RUN/role_policy.json")
SESSION=${SESSION:-epm-$DOMAIN}
SOCLAAS=" ${SOCLAAS_EPISODES:-} "
[ "$SOCLAAS" = "  " ] || [ -f "$CONFIG/soclaas.env" ] || die "SOCLAAS_EPISODES set but no $CONFIG/soclaas.env"
mapfile -t TAGS < <(cd "$REPO/pipeline" && "$PY" -c "import common,sys; print('\n'.join(common.episode_tags(sys.argv[1])))" "$DOMAIN")
mkdir -p "$RUN/results"
tmux has-session -t "$SESSION" 2>/dev/null || tmux new-session -d -s "$SESSION" -n main -c "$REPO"
backends="{"
for ep in "${TAGS[@]}"; do
  if [[ $SOCLAAS == *" $ep "* ]]; then b=soclaas; else b=local; fi
  backends+="\"$ep\": \"$b\", "
  tmux new-window -d -t "$SESSION" -n "ep$ep" -c "$REPO" \
    "env MAX_PARALLEL=${MAX_PARALLEL:-0} AMEM_PYTHON=$PY bash $REPO/pipeline/run_episode.sh $RUN $b $ep; exec bash"
done
echo "${backends%, }}" | "$PY" -c "import json,sys; json.dump(json.load(sys.stdin), open(sys.argv[1], 'w'), indent=1)" "$RUN/backends.json"
echo "launched ${#TAGS[@]} $DOMAIN episodes in tmux session $SESSION (MAX_PARALLEL=${MAX_PARALLEL:-0})"
