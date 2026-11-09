#!/usr/bin/env bash
# Offline smoke test of a run directory's frozen policy: scripted processors, hash
# embeddings, no model calls, first episode, two boundaries. It proves that the host,
# the policy and the harness load together and that the run's manifest records exactly
# this role_policy.json. It says nothing about model quality.
#
#   bash pipeline/smoke_test.sh runs/<domain>         # writes runs/<domain>/smoke-test/
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
RUN=$(cd "${1:?usage: smoke_test.sh RUN_DIR}" && pwd)
[ -f "$RUN/role_policy.json" ] || die "no $RUN/role_policy.json (run pipeline/freeze_policy.py first)"
(cd "$RUN" && sha256sum -c --quiet role_policy.json.sha256) || die "role_policy.json does not match its sha256"
DOMAIN=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['domain'])" "$RUN/role_policy.json")
FIRST=$(cd "$REPO/pipeline" && "$PY" -c "import common,sys; print(common.episode_tags(sys.argv[1])[0])" "$DOMAIN")
rm -rf "$RUN/smoke-test"; mkdir -p "$RUN/smoke-test"
cd "$REPO"
env CUDA_VISIBLE_DEVICES= timeout 1800 "$PY" -m evaluation.gatemem_medical --offline --domain "$DOMAIN" \
  --gatemem-repo "$DATA" --env-file /dev/null --permission-mode inference --permission-version 19 \
  --answer-validation-mode enforce --role-policy "$RUN/role_policy.json" --learned-access-sources 2 --reply-access \
  --episode-contains "$FIRST" --max-boundaries 2 --run-dir "$RUN/smoke-test/run" > "$RUN/smoke-test/run.log" 2>&1 \
  || die "offline smoke test failed (see $RUN/smoke-test/run.log)"
want="policy-sha256:$(cut -d' ' -f1 "$RUN/role_policy.json.sha256")"
got=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['access_policy']['role_tables'])" "$RUN/smoke-test/run/manifest.json")
[ "$got" = "$want" ] || die "smoke test loaded a different policy ($got)"
echo "offline smoke test passed: $DOMAIN $FIRST, policy $want"
