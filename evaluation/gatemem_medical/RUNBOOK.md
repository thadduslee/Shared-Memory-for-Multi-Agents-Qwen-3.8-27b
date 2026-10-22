# Standalone privacy-aware A-MEM: full GateMem run

This runbook now targets the implemented runtime in `agentic_memory/privacy/`.
It does not launch, import, or execute the old Auto Research online system.
GateMem supplies the benchmark data and native scorer, not the memory backend.

## Start in terminal

The installed Python environments and cached model snapshots used below already
exist on this host. They supply dependencies only. All memory and runner source
code comes from `/home/thaddus/a-mem`.

```bash
cd /home/thaddus/a-mem

# Start these only if their ports are not already serving the named models.
tmux new-session -d -s amem-model -c /home/thaddus/a-mem 'bash scripts/serve_model.sh'
tmux new-session -d -s amem-judge -c /home/thaddus/a-mem 'bash scripts/serve_judge.sh'

# Inspect startup logs; allow initialization to finish before preflight.
tail -f runs/model-server.log runs/judge-server.log
```

After both logs report application startup complete, stop `tail` with Ctrl+C:

```bash
tmux new-session -s amem-eval -c /home/thaddus/a-mem
```

Inside that session:

```bash
bash scripts/run_gatemem.sh --run-dir runs/gatemem-medical-full
```

The launcher runs the domain-free security tests, verifies the local processors,
then starts every boundary with semantic evolution enabled. It has no development
slice or boundary cap. Credentials and endpoints come from the private A-MEM
`.env` (see `.env.example`). Both generation and judging use localhost by default;
no benchmark payload is sent to an external judge. Do not source `.env` as shell
code. The Python loader accepts only explicit `AMEM_` keys.

Detach using Ctrl+B, then D; reattach using `tmux attach -t amem-eval`.
After interruption, use the exact same run directory:

```bash
bash scripts/run_gatemem.sh --run-dir runs/gatemem-medical-full --resume
```

Resume rejects changed source data, engine/adapter/policy code, processor IDs or
causal run settings. Do not edit code midway through a fixed-version experiment.
To test revised code, start a new run directory. A model-request retry may repeat
an external computation after a crash, but committed source membership and official
predictions are idempotent. Run locks prevent concurrent writers.

## Nodes and models

The new runtime is a guarded pipeline, not the old Architect/Developer/Critic
code-repair loop:

| Stage | Implementation / processor |
| --- | --- |
| Fixture source adapter and boundary controller | New deterministic Python code in `evaluation/gatemem_medical` |
| Canonical policy authority, scoped sessions, group grants and lifecycle | New deterministic SQLite-backed core |
| Source ingestion and source-addressable projections | Generic text/JSON adapters, plus medical fixture configuration |
| Authorized candidate selection and exact vector ranking | Canonical checks first; local `all-MiniLM-L6-v2`, 384 dimensions |
| Semantic metadata and directed-link proposals | Local `Qwen/Qwen3.8-27B` on GPUs 4 and 5, port 8000 |
| Evolution validation, protected overlays, edges and lineage | New deterministic core; the model cannot write permission authority |
| Answers from permitted evidence | Local `Qwen/Qwen3.8-27B`, independent stateless calls |
| Final policy/dependency release barrier | New deterministic core, buffered output |
| Independent evaluation | Native GateMem rules plus separate local `Qwen/Qwen3-14B` on GPU 3, port 8001 |

The full evidence given to an evolution call constrains every generated overlay
and edge. The public/base note is immutable. Private enrichment stays in separate
protected artifacts, including its own vector. Readable edges never grant access.
No private source is used to update shared model weights.

## Complete incremental protocol

The pinned medical data contains 21 episodes, 4,294 turns and 579 original queries.
There are 549 distinct query boundaries and 18 data-only tail boundaries: **567
boundaries total**. Every boundary introduces at least one new source turn.
`boundary_manifest.jsonl` and `schedule_summary.json` record the reviewed schedule.
The complete manifest belongs only to the evaluator; it is never model context.

At each boundary:

1. Take only the new turn slice since that episode's prior committed boundary.
2. Apply verified control events and ingest protected observations/notes.
3. Embed notes locally and perform governed semantic evolution.
4. Commit the boundary's canonical source membership and evolution state.
5. Ask every original query at that boundary against the same memory state.
6. Record all first-attempt predictions before invoking the independent scorer.
7. Commit scores and move to the next boundary.

Memory accumulates across boundaries subject to policy and lifecycle changes.
Each original query is answered once, at its assigned boundary. Reports accumulate
those saved scores; earlier queries are not rerun against later memory. For example,
boundary 1 ingests 54 turns and asks one query, boundary 2 adds 13 turns and asks
one new query, and boundary 3 adds 16 turns and asks two new queries. Replaying old
queries against later memory would be a separate longitudinal evaluation, not an
original-checkpoint score at its original source prefix.

Multiple queries at one source turn share the boundary; do not split them and
fabricate arrivals. Tail boundaries ingest remaining data without inventing
extra queries. Checkpoint suffix order is not chronological order. Episode facts
are compartment-isolated. Queries and score annotations never become shared notes.
There is no autonomous code/schema rewriting during this experiment.

## Outputs and completion

Each run contains:

- `manifest.json`: code, fixture-policy, dataset and model identity fingerprints.
- `schedule.json`: all boundaries and their exact source slices.
- `memory.sqlite`: protected canonical observations, notes, policies, groups,
  edges, overlays, provenance, tombstones and representation index.
- `evaluation.sqlite`: durable boundary state and immutable first predictions.
- `official_predictions.jsonl`: original predictions with the actual exposed
  evidence and dependency IDs, in evaluation-only storage.
- `scores.jsonl`: native auxiliary and separate model judgments.
- `boundary_results.json`: per-boundary ingestion/query counts and elapsed time.
- `summary.json`: exact coverage, U/A/F/OR/MGS, action accuracy, and end-to-end
  leakage metrics that count forbidden model-context exposure as well as answers.

`complete=true` requires all 579 original queries, all 567 boundaries, all 4,294
source turns, exact ID sets, and a score for every query. Zero-value rates for
unobserved categories are replaced by nulls. Transport/schema errors stop the run
with resumable state, rather than counting missing evaluations as passes.
The local judge uses constrained JSON with a category-specific boolean verdict,
plus independent schema validation and at most three schema attempts. Invalid or
missing verdicts never become passing scores. If a repair changes pinned code,
preserve the interrupted run and use a fresh directory for the revised experiment.

View progress in another terminal:

```bash
cd /home/thaddus/a-mem
cat runs/gatemem-medical-full/summary.json
```

## Independent OpenRouter re-judge

To preserve the original experiment, re-judging writes a separate sibling
directory and never edits the source run's immutable predictions or scores. Add
your OpenRouter key to the private `/home/thaddus/a-mem/.env` file (not this
runbook and not a tracked file):

```dotenv
AMEM_OPENROUTER_API_KEY=your_openrouter_key_here
AMEM_OPENROUTER_JUDGE_MODEL=openai/gpt-4.1
```

Then validate the credentials with one small request and run the complete
re-judge. The model receives the existing judge case only; it does not rerun the
memory system or regenerate any prediction.

```bash
cd /home/thaddus/a-mem
/home/thaddus/gatemem/.venv-nb/bin/python -m evaluation.gatemem_medical.rejudge \
  --source-run runs/gatemem-medical-full-20261020-v3 --preflight

/home/thaddus/gatemem/.venv-nb/bin/python -m evaluation.gatemem_medical.rejudge \
  --source-run runs/gatemem-medical-full-20261020-v3
```

The output defaults to
`runs/gatemem-medical-full-20261020-v3-gpt-4.1-openrouter/`. It contains its
own manifest, SQLite progress journal, `scores.jsonl`, and `summary.json`.
If interrupted, rerun the second command with `--resume`; committed judgments
are not submitted again. Use `--max-checkpoints N` for a bounded paid trial.

The core suite is independent of model services:

```bash
python3 -m unittest discover -s security_tests -v
python3 -m evaluation.gatemem_medical --plan
```

Explicit `--offline` mode uses deterministic test processors to verify the entire
579-query schedule and recovery without any model. Its results are labeled
`offline-functional-test`; they are **not** real-model utility/privacy measurements.
The default launcher always uses real models. `--no-evolution` is an explicitly
labeled ablation. `--max-boundaries N` limits additional boundaries per invocation,
not the eventual dataset; resume without that limit to finish the full set.

## Scope and limitations

This implements the single-service, exact-search architecture, not every possible
production deployment extension. Text and JSON parsing are included. Other MIME
types require separately registered approved parsers and otherwise quarantine.
Physical isolation, encryption domains, distributed release fencing, streaming,
IdP integration, remote processor contracts, model unlearning and backup erasure
are deployment extensions, not demonstrated guarantees.

The fixture identity and authority adapter is deliberately explicit. Subject
bindings live in `fixture_policy.json`; clinical roles/predicates are absent from
the core. Missing clinical assignments deny access rather than inventing them.
Natural-language consent/deletion interpretation is bounded and conservative;
unsupported language can cause excessive denial or benchmark errors. This is
measured openly, not fixed by exposing hidden scoring annotations. The family
logistics projection exposes only exact configured date/time spans under a
separate authoritative field policy, not a model's purported sanitized summary.

Canonical resources have immutable IDs/revisions; replacing a source uses a new
ID and explicit lifecycle control. Deletion blocks and removes current dependent
representations, but physical SQL page/WAL/backups and externally retained source
fixtures require separate retention management. Evaluator result files are
protected operator artifacts, not a public query/export endpoint. Live policy
changes affect future core reads/releases; already delivered bytes cannot be
recalled. Recorded IDs/digests preserve lineage without keeping raw model prompts.
The trusted host issues sessions after authenticating its application caller;
source text cannot invoke that authority API. No production authentication claim
is made merely by accepting a fixture requester ID in this evaluation adapter.
