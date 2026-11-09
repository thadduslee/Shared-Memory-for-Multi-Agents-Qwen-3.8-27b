#!/usr/bin/env bash
# Starts all 21 medical episodes in tmux session medical-new-system, whose "model"
# window already serves vLLM on GPUs 0,3. 001-006 on SOCLAAS, 007-021 on local vLLM.
# (medical_domain_original used 001-005 on SOCLAAS.)
ROOT=/home/thaddus/v19-evaluations/medical_domain_new_system; S=medical-new-system
[ -f $ROOT/role_policy.json ] || { echo "no role_policy.json; not launching"; exit 1; }
sha256sum -c $ROOT/role_policy.json.sha256 || { echo "role_policy.json changed; not launching"; exit 1; }
curl -sf -m 5 -H 'Authorization: Bearer dummy' http://127.0.0.1:18000/v1/models >/dev/null || { echo "local vLLM not up; not launching"; exit 1; }
[ -d $ROOT/results ] && [ -n "$(ls -A $ROOT/results 2>/dev/null)" ] && { echo "results/ not empty; not launching"; exit 1; }
for n in 001 002 003 004 005 006; do tmux new-window -d -t $S -n ep$n -c "$ROOT" "$ROOT/run-episode.sh soclaas _${n}_"; done
for n in 007 008 009 010 011 012 013 014 015 016 017 018 019 020 021; do tmux new-window -d -t $S -n ep$n -c "$ROOT" "$ROOT/run-episode.sh local _${n}_"; done
tmux list-windows -t $S -F '#{window_name}' | wc -l
sleep 120
pgrep -u thaddus -fc "evaluation.gatemem_medical"
