# Permission inference repair and speed validation

The v2 run ingested raw observations but never established assignments or bound
information, so search had no indexed resources and the runner returned its
hardcoded `no_memory` response. Its first five boundaries took 1,596, 841, 1,106,
379, and 1,661 seconds (54, 13, 16, 5, and 20 new turns respectively).

## Changes

- Generate the operation and permission basis before their dependent fields.
  Explicit assignments cannot contain an unrelated task activity ID.
- Explain subject acceptance of a worker's task separately from a worker's
  unsupported self-assertion. State the source-grounded self-use convention in
  the adapter bootstrap: requesting/accepting help permits one's own access to
  responsive information for that activity. Identity ownership alone grants none.
- Compile an empty, activity-bounded subject self-use assignment after validated
  authenticated subject acceptance when the bootstrap explicitly enables it.
  Copy no worker fields; each subject field still requires a separate binding.
  This closes the observed gap where only the clinician received clinical fields.
- Require model-generated grants to be active or remain observation/no grant,
  instead of indefinitely attaching information to unsearchable proposed records.
- Pair resource/authority and corroboration references in the network schema,
  preventing structurally impossible choices before a costly repair cycle.
- Require restrictions to have `effect: deny` and checks distinguishing the
  actual prohibition from the permitted subset.
- Retain verbatim original scope evidence on assignments. New information
  bindings must cite that original authorization, and separately explain why
  their selected text is within scope. An absence of denial is not authorization.
- Use short source aliases on the network wire, expand them before host
  validation, and keep canonical IDs in audit/storage. Keep all live source text,
  authority, evidence, restrictions, and validity; omit repeated diagnostic prose
  from the ledger returned to the model.
- Select unique exact source quotes on the compact wire; resolve them to character
  spans in the host. Ambiguous or inexact text is rejected, never fuzzy matched.
- Put the stable source prefix ahead of changing state, emit compact JSON, and
  explicitly enable vLLM prefix caching for the hybrid model. Runtime logs show
  actual cache hits; the previous service had caching disabled.
- Reuse embeddings for unchanged immutable fields. Send only newly created fields
  to evolution while retaining every target in runtime assignments.
- Alias evolution note IDs inside the trusted network serializer, restoring
  canonical IDs before processing tickets are issued. Target metadata updates at
  new notes while retaining older neighbors as possible graph edge endpoints.
- Log source wall time and permission token usage separately from evolution.
  Summaries include searchable resources, active assignments, elapsed inference
  time, and tokens, including rejected and repair attempts.

Source event boundaries remain independent of checkpoint placement. No future
turns, evaluator entities, expected answers, or scoring labels enter inference.
Old official predictions and stores were preserved; repaired results use a new
run directory and permission version.

## Validation

- 144 deterministic privacy/security tests and 29 evaluator tests pass.
- Added regressions for subject corroboration, rejecting allowances as denies,
  anchoring bindings to original scope, exact/ambiguous quotes, source alias
  expansion, schema discriminator ordering, and reuse of existing embeddings.
- `semantic_smoke.py` runs the real local model on a new release-planning
  conversation. All four checks passed: self-access to responsive instructions,
  a helper's permitted pickup information, exclusion of private account/contact
  information from the helper, and empty access for an outsider. The full
  permission smoke took 129 seconds over four source turns. See
  `semantic-smoke.json` for the retrieved text and ledger.
- A read-only replay of the old t005 input took 132.1 seconds and generated 3,318
  output tokens, including a restriction on the permitted logistics scope.
  The initial compact repair took 38.9 seconds and 991 output tokens, producing
  an explicit logistics grant and a clinical-only denial. This is a diagnostic
  example (about 3.4x faster), not a whole-run speed or accuracy claim. The final
  final contract adds original-scope checks beyond that initial probe.
- Early replay inspections detected an overbroad logistics interpretation; those
  smoke runs were stopped and preserved. The final contract and bootstrap include
  the tighter scope and self-use checks above.
- `clinical_answer_probe.py` replayed the original medication question through
  the actual answer function using those patient-authorized fields. The independent
  judge marked action and utility correct, covering all seven required items;
  over-refusal is false. This is an answer-layer diagnostic using retrieved evidence,
  not a new official full-prefix checkpoint. See `clinical-answer-probe.json`.
- The focused initial clinical consultation initially returned no patient evidence
  despite eight clinician-bound fields. With the compiled self-use rule it returns
  eight patient-authorized fields, including the complete medication plan and all
  doses; family clinical retrieval remains empty. See `clinical-smoke.json`.
  This focused source-subset diagnostic is not an official full-prefix score.
- On comparable populated early sources, an intermediate replay took 57.9 seconds
  at t008, while the replay with the evolution optimizations took 29.2 seconds;
  that replay also avoids the broader logistics binding seen earlier. Full-run
  speed remains to be measured because useful permissions create real evolution
  work that the all-empty original run never performed.
- `git diff --check` passes. The separate legacy `tests/` suite could not import
  under system Python because that interpreter lacks `litellm` and `pytest`;
  it covers the older memory system, not this privacy runtime.

## Active replay

The full evaluation runs in tmux `amem-raw-stream`, writing to
`runs/gatemem-medical-raw-stream-v6/`. Both memory evolution and the independent
judge remain enabled. The initial boundary is still ingesting; whole-run medical
scores and boundary timing are pending. Completed early permission jobs have not
shown the earlier structural repair failures. Its startup wrapper runs all 173 relevant deterministic
tests before preflight. Full medical utility, privacy, and boundary speed must be
assessed from this new run, not inferred from the smoke checks.
