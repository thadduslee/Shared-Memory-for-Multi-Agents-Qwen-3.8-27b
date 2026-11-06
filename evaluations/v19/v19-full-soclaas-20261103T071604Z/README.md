# Active fresh V19 SOCLAAS evaluation

This attempt started on 3 November 2026 at 15:18 Singapore time. It is running in
the existing session; the log window follows this attempt's log.

```bash
tmux attach -t v19-full-soclaas-20261103T064703Z
tail -F /home/thaddus/v19-evaluations/v19-full-soclaas-20261103T071604Z/run.log
```

All chat roles, including the separate verifier and judge calls, use
`qwen3.8:27b` at `http://localhost:19000/v1`. Local MiniLM embeddings are
unchanged. The judge uses the same model family as generation, which is recorded
in `launch-manifest.json`. No local serving process or GPU allocation is used.

The prior attempt's strict-schema probe succeeded in 101.288 seconds. Its
standalone preflight subsequently failed on intermittent gateway 502 errors.
A new minimal request matching the user's approach also succeeded in 111.545
seconds. This fresh attempt uses the prior successful schema probe rather than
repeating the standalone preflight gate. All original preflight logs remain in
`../v19-full-soclaas-20261103T064703Z/`.

The request timeout is now 900 seconds. This allows slow successful requests;
it cannot prevent a gateway from returning its own earlier HTTP 502. The actual
runner's existing RecoveryController retries transient transport failures on the
same source/query/judge item. It does not advance a failed causal boundary or
reset semantic repair budgets. Invalid contracts and permanent errors retain
their existing bounded or stopping behavior. No automatic store reset is used.

The full target remains 21 episodes, 4,294 raw source turns, 567 boundaries and
579 scored checkpoints. Evolution and answer validation enforcement remain on;
no subset or resume flag is supplied. New stores are in `results/`. The optional
task registry and host gateway remain disabled for this raw-stream cohort, which
does not supply authenticated host assignment events.

`status.json` reports phase and last committed boundary coverage. `run.log`
retains ingestion/recovery/scoring progress and periodic coverage heartbeats.
`results/official_predictions.jsonl`, `results/scores.jsonl` and
`results/summary.json` are refreshed after committed boundaries. The first
boundary ingests 54 turns before its first checkpoint, so scores remain empty
until that work completes. The supervisor prints final metrics and requires full
coverage before reporting completion. `exit-code.txt` appears when it exits.

The two slow probes establish connectivity; they do not establish full-run speed,
quality, false rejection rate or V19-equivalent latency. Those remain to measure.
