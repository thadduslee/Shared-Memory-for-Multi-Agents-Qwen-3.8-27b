# Fresh V19 GPU evaluation

Session: `v19-full-gpu345-20261103T061619Z`

```bash
tmux attach -t v19-full-gpu345-20261103T061619Z
tail -F /home/thaddus/v19-evaluations/v19-full-gpu345-20261103T061619Z/run.log
```

The running supervisor waits for physical GPUs 3, 4 and 5 to become available.
It does not terminate other users' jobs. Three consecutive availability checks
are required. Qwen3.8-27B runs on GPUs 4–5; Qwen3-14B scores on GPU 3. Device
UUIDs are recorded and used to avoid CUDA device-order ambiguity.

The full schedule is 21 episodes, 4,294 raw source turns, 567 boundaries and 579
checkpoints. No episode/checkpoint/boundary cap or resume is configured. Source
inference, evolution, enforced answer evidence verification and independent
scoring are enabled. The raw stream contains no trusted host assignment events;
the optional task registry and host gateway remain disabled for this cohort.

`status.json` records the current phase and periodically records coverage.
`run.log` retains queue, preflight, ingestion, checkpoint and scoring progress.
Separate `memory-server.log` and `judge-server.log` retain serving diagnostics.
The corresponding tmux windows follow those files. `exit-code.txt` is written
when the supervisor exits, including on failure.

After the endpoints pass preflight, `results/` is created with fresh evaluation
and memory stores. `results/official_predictions.jsonl`, `results/scores.jsonl`
and `results/summary.json` are refreshed as boundaries commit. The last summary
and its metrics are also printed to `run.log`. The supervisor requires complete
579/579 scored-checkpoint, 567-boundary and 4,294-source-turn coverage before
reporting completion. Scores are not available while the job is queued.

Workspace and model paths, Git revision, exact run settings and schedule counts
are in `launch-manifest.json`, `local.env`, `schedule-plan.json` and
`source-hashes.json`. Runtime files are rechecked after queueing and preflight;
changes cause a stop rather than an implicit mixed-code run. Historical runs
and stores are never read as initialization or overwritten. All model calls use
the pinned localhost endpoints; no OpenRouter key/configuration is inherited.

The implementation's offline tests passed before launch. Real-model quality,
false rejection and latency measurements remain pending until actual execution;
this job's local independent judge is not a substitute for human adjudication
or a matched historical latency comparison.
