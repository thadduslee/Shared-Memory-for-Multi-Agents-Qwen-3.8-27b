# v8 t078 schema-size recovery

The saved run stopped in boundary 3 while processing source t078. A replay of
the failed request against localhost returned HTTP 400 with the response body
`Grammar error: schema too large`. Its permission job remained `processor_error`;
77 previous jobs and two immutable predictions/scores had committed.

`capture_failed_request.py` reconstructs the request on copied databases. Its
instruction/payload/schema hash matches the failed attempt exactly:
`4912bb1449f8604d1b3a44efc1f652bd4e63c56392e5bc2e61f0d1910270c4f9`.

The model launcher now puts `scripts/vllm_schema_support` on the serving
processes' Python path. Its `sitecustomize.py` hook factors repeated schema nodes
into local `$defs`/`$ref` before llguidance compiles them. The original schema is
459,855 bytes; the shared form is 85,346 bytes with 29 definitions. Expanding
those references reproduces the original schema, including property/enum order.
It does not truncate evidence, remove constraints or change the client request.
Schemas with existing IDs, references, definitions or decoder extensions pass
through untouched to avoid changing reference resolution.

Checks:

- The unmodified schema reproduces the llguidance compiler error on CPU.
- With the hook, the exact schema compiles without warnings. The model's local
  tokenizer passes 316 next-token-mask/consumption steps and accepts a complete
  valid object. See `guidance-regression.json`.
- A larger source enum with 512 entries also compiles without warnings; the
  largest episode in this evaluation has 225 source turns. This checks source
  enum growth, not every possible future permission-ledger configuration.
- All 172 domain-free security tests pass, including three serving-compaction
  regressions for exact schema expansion, scoped references and literal data.
- The evaluation code digest matches the original manifest. Neither it nor the
  dataset, engine, adapter, prompts, schemas or completed predictions was edited.
- The model service was restarted in its existing `amem-model` pane with the
  same model, Guidance backend and GPU configuration. Both local model services
  returned HTTP 200 from `/health` before resumption.

`resume-http400.sh` in the original run directory first invokes
`recover_pending.py`, which restores t078's original source timestamp and checks
that its first retry has the failed request's hash. It then invokes the normal
runner with the original arguments and `--resume`. Recovering the pending job
first prevents earlier turns replayed within boundary 3 from changing its clock.
The runner performs source evolution normally using deterministic existing job
IDs. Both commands run in the original `amem-v7-full` pane `%7`.

The original model log and three databases are backed up in this diagnostic
directory. `before.json` preserves completed job hashes/proposals/changes and
official predictions/scores for comparison. The original exit status is archived
in the run directory. `schema-serving-recovery.json` there fingerprints the
serving changes separately from its original immutable manifest. Decoder grammar
representation can affect generated sequences; the full benchmark remains an
ongoing run rather than a completed quality validation.

## Live result

The original t078 request returned HTTP 200 on the restarted service. The host
rejected its first generated proposal because a question cannot establish a
factual memory. The usual correction retry passed, publishing zero changes;
t078 is now complete with three total attempts including the original HTTP 400.
See `pending-recovery.json`. No validation check was bypassed.

The normal runner resumed boundary 3 in pane `%7`, replayed t068–t078
idempotently, and started t079. `preserved-work.json` confirms that all 77 prior
completed job hashes/proposals/changes and both official predictions/scores are
unchanged, as is the manifest. The evaluation remains active; no full benchmark
completion or quality result is claimed here.
