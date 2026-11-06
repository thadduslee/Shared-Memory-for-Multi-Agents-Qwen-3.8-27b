# Full V19 evaluation through SOCLAAS

```bash
tmux attach -t v19-full-soclaas-20261103T064703Z
tail -F /home/thaddus/v19-evaluations/v19-full-soclaas-20261103T064703Z/run.log
```

The supervisor uses `http://localhost:19000/v1` and `qwen3.8:27b` for all
completion roles: source interpretation, generation, evidence verification,
evolution and scoring. Embeddings remain local MiniLM because this Qwen model
advertises chat capabilities, not embeddings. The judge is a separate scoring
call/prompt using the same model as the answerer; this is not an independent-model
judge configuration. Those settings are explicit in `launch-manifest.json`.

The earlier GPU345 supervisor was interrupted while still waiting for other
users' GPU jobs. No benchmark results were produced there. This SOCLAAS job does
not reserve GPUs or start any local serving processes.

At setup, `/models` advertised the requested Qwen model, but `/chat/completions`
returned HTTP 502, reporting an upstream connection timeout. An empty Bearer
header instead returned HTTP 401; the production client now omits Authorization
when no key is configured. Configured-key requests retain their original header.
The transport regression suite passed 23 tests.

`status.json` reports the current phase. While SOCLAAS is unavailable, a synthetic
strict-schema readiness probe runs at most once per 60 seconds after each failed
probe finishes. Its result/timing is retained in `readiness-probes.jsonl` and
`run.log`; it contains no benchmark data. Once readiness passes, the actual
production preflight runs, then the full evaluation starts with fresh stores.
Any later evaluation failure is logged without silently resetting stores or
changing the implementation. `exit-code.txt` is written when the supervisor exits.

The complete target is 21 episodes, 4,294 source turns, 567 boundaries and 579
scored checkpoints. There are no subset limits, no reused memory stores and no
resume flag. Evolution and answer validation enforcement remain enabled. Runtime
hashes and Git revision are checked before and after waiting and preflight.
The raw-stream task registry/host gateway remain disabled; no assignment events
are derived from evaluator-only annotations.

After preflight, `results/official_predictions.jsonl`, `results/scores.jsonl` and
`results/summary.json` are refreshed as boundaries complete. The final scored
summary is printed to `run.log`, and completion requires exact full coverage.
No benchmark scores or latency/quality results exist while the service is pending.

Code revision: `02a7c8f643f0cdf5697ec3cbc5cf5bbe64705b5b`.
The patch includes the keyless transport fix. Prior policy/verification tests and
offline replay results remain in the isolated workspace's
`analysis/v19-answer-validation/`. Real-model accuracy, false rejection and matched
historical latency comparisons remain to be measured.
