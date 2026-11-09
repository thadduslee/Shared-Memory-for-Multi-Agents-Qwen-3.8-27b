#!/usr/bin/env bash
# usage: run-episode.sh <local|soclaas> <_NNN_>
# local = vLLM on GPUs 0,3 (port 18000). soclaas = http://localhost:19000/v1.
# Same as medical_domain_original except: --role-policy, --learned-access-sources 2, --reply-access.
# A failed run resumes from its journal, up to 5 attempts.
MODE=$1; EP=$2; ROOT=/home/thaddus/v19-evaluations/medical_domain_new_system; D=$ROOT/results/$EP; umask 077; mkdir -p "$D"
export CUDA_VISIBLE_DEVICES= OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 PYTHONDONTWRITEBYTECODE=1
cd "$ROOT/frozen-source"
for attempt in 1 2 3 4 5; do
  if [ "$MODE" = local ]; then ENVF=$ROOT/model.env
    until curl -sf -m 5 -H 'Authorization: Bearer dummy' http://127.0.0.1:18000/v1/models >/dev/null; do sleep 15; done
  else ENVF=$ROOT/soclaas.env
    until curl -sf -m 5 http://localhost:19000/v1/models >/dev/null; do sleep 15; done
  fi
  RESUME=(); [ -f "$D/manifest.json" ] && RESUME=(--resume)
  /home/thaddus/gatemem/.venv-nb/bin/python -u -m evaluation.gatemem_medical --env-file "$ENVF" \
    --gatemem-repo /home/thaddus/a-mem-variant/evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/frozen-dataset \
    --permission-mode inference --permission-version 19 --answer-validation-mode enforce \
    --role-policy "$ROOT/role_policy.json" --learned-access-sources 2 --reply-access \
    --episode-contains "$EP" --run-dir "$D" --answer-validation-log "$D/raw-replies.jsonl" "${RESUME[@]}" 2>&1 | tee -a "$D/run.log"
  code=${PIPESTATUS[0]}
  [ "$code" -eq 0 ] && break
  echo "attempt $attempt exited $code at $(date -u +%FT%TZ)" | tee -a "$D/run.log"
  [ "$attempt" -lt 5 ] && sleep 60
done
echo "$code" > "$D/exit-code.txt"; echo "finished $EP exit $code"; exec bash
