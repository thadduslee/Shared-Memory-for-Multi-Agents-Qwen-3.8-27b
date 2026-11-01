# Standalone privacy-aware A-MEM: full GateMem run

Version 15 is the default. Use a fresh directory with `--permission-version 15`.
See [combined incremental processing](../../INCREMENTAL_PERMISSIONS.md) for empty
access bootstrap, opaque identities, incremental review, bounded repair and the
validation audit. Generic applications can use `PermissionUpdater` and
`PermissionQueryService`; `examples/incremental_memory.py` imports no benchmark code.

For a full fresh OpenRouter run, put the single key in the private root `.env`:

```dotenv
AMEM_OPENROUTER_API_KEY=your-key-here
```

Then run:

```bash
HF_HOME=/home/thaddus/.cache/huggingface /home/thaddus/gatemem/.venv-nb/bin/python -u \
  -m evaluation.gatemem_medical.openrouter --run-dir runs/FRESH_OPENROUTER_DIRECTORY
```

This launcher fixes the memory/answer model to `qwen/qwen3.8-27b` and the judge to
`openai/gpt-4.1`, both through `https://openrouter.ai/api/v1`. It verifies both
response model IDs and structured JSON before starting. Local endpoint settings
in `.env` are preserved. It processes all 4,294 source turns, 579 checkpoints and
567 boundaries in the standard causal order, with evolution enabled and empty
initial requester grants. The key is never written into run artifacts. Optional
`--resume` continues that same run without rewriting committed predictions/scores.
Provider routing requires requested parameters and prefers latency; Qwen uses
OpenRouter's `reasoning.enabled=false`, preserving the previous fast decoding mode.
This changes the transport setting, not whether the model interprets source data.

Version 14 remains selectable. See [durable candidates](../../DURABLE_FACTS.md) for its
separate extraction ledger, focused scope reviews, metrics and limitations. Start
a fresh run with `--permission-version 14`; do not reinterpret historical v13
artifacts in place. Version 13 remains explicitly selectable and uses [single-pass permission deltas](../../PERMISSION_INFERENCE.md#single-pass-permission-deltas-version-13):
six recent events plus concise current policy, with unconfirmed tasks stored as
tentative activities. In one response per turn, the model reports independent
`permission_changes` and `new_facts`, with per-assignment scope and restriction
checks. Established tasks track requester, worker and beneficiary separately and
assess their access; later task facts must assess every active allowed recipient.
Missing allowed facts yield “I don't have that information”; authorization refusals
require a host policy assessment. There are no focused reassessments or historical
retrieval. Identical overlap IDs are deduplicated before validation. Invalid proposals
receive validation feedback within a durable three-attempt budget. After three
rejections, the v13 adapter audits the permission turn as skipped and continues to
the next source. No rejected changes publish. The skipped turn retains a protective
access hold on its episode, and affected checkpoint outputs have `context_complete:
false`; the summary lists skips and reports degraded permission inference separately
from source/boundary/checkpoint coverage. Skipped jobs are not retried after reopening.
Original evidence stays host-side and normal field/access checks remain.
Use a fresh run directory:

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 13 \
  --run-dir runs/gatemem-medical-raw-stream-v13
```

The permission output limit is `AMEM_PERMISSION_MAX_TOKENS` (default 3072).
The validation report distinguishes scripted correctness checks from local-model
measurements; a shorter input is not by itself a latency or accuracy result.

Version 12 remains available explicitly. It adds [durable task resolution](../../PERMISSION_INFERENCE.md#durable-task-resolution-version-12)
to the six-turn sliding window and concise current access state. Affected candidates
need explicit resolutions; accepted worker tasks require their own narrow grants
and necessary bindings. Conditional focused reassessment and targeted historical
fact lookup have fixed budgets. Historical facts cannot supply fresh authority.
It does not replay historical authorization evidence or binding history.
The current policy table is `permission_access_state` in
`memory.privacy.sqlite`; evidence and justification remain in separate host audit
tables. New grants must encode their actual scope limits in the policy itself.
Use a fresh run directory. Existing populated v10 stores cannot be relabeled v12.

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 12 \
  --run-dir runs/gatemem-medical-raw-stream-v12
```

Deferred enrichment and durable repair limits from v10 still apply. See the
[scripted tests and isolated live-model results](../../analysis/task-resolution-v12-20261029/REPORT.md)
for current evidence and limitations. Explicit `--permission-version 11` retains
the strict six-turn contract. The offline
[size comparison and sample current ACL](../../analysis/sliding-permissions-v11-20261029/VALIDATION.md)
do not establish live latency or semantic accuracy.

The following sections describe earlier versions and their retained behavior.

Version 10 adds [final-state permission validation, durable repair limits,
compact deltas and deferred enrichment](../../PERMISSION_INFERENCE.md#per-turn-deltas-and-deferred-enrichment-version-10).
Version 10 uses permission version `10`. Permissions and searchable fields
still commit every turn, starting from no requester grants. Optional enrichment
runs in durable four-field batches after answering/scoring each boundary; partial
batches flush at invocation end. This changes the enrichment schedule and model
contract, so use a fresh run directory. Prior v9 artifacts remain preserved.

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 10 \
  --run-dir runs/gatemem-medical-raw-stream-v10
```

For the legacy workflows described below, three invalid permission proposals exhaust a durable per-job budget; recovery
records `blocked` and exits while requester access stays blocked. Reopening does
not reset that budget. Invalid/truncated responses also have bounded repairs;
permanent HTTP errors stop immediately, while transient network/server outages
can wait and retry. Inspect `recovery.json` and the saved attempt validation before
an explicit repair. Optional enrichment outages leave `evolution.queue.pending`
items and do not send ingestion back into the permission retry loop. A run can
finish its source/query coverage with pending enrichment; inspect
`evolution.enrichment_complete` separately.

The version-specific descriptions below document earlier experiments. Their
latencies and model-quality results are not measurements of version 10.

Version 9 replaces full-prefix model replay with
[incremental context and targeted evidence retrieval](../../PERMISSION_INFERENCE.md#incremental-context-version-9).
Recent turns, relevant control records, original authorization/condition evidence,
pending decisions, all denials and a compact ledger index form the initial input.
Missing sources/records can be requested before the final atomic delta. Host
validation still uses the complete canonical ledger and live causal prefix.
Use a fresh directory and `--permission-version 9` for this change; preserve all
existing v7/v8 predictions and databases. The older diagnostics below describe
their original versions and are not measurements of v9 model quality.

Version 8 fixes the v7 `t065` question-binding repair loop. Compact model input
marks candidate assertion ranges and excludes question-only sources from factual
binding choices; questions remain permission evidence. The host still rejects
question spans, including crops inside mixed events. Repair feedback explicitly
requires removing a binding when no asserted range exists. Historical binding
claim evidence is omitted only from the model wire; canonical evidence, original
scope text, full live sources and all restrictions remain in storage and available
to validation. Shorter proposal explanations reduce generation work. Progress
now separates permission time/attempts from evolution time.

The v8 change required a fresh directory with `--permission-version 8`. Preserve
failed v7/v8 directories; `--resume` intentionally rejects an implementation change.
See [the measured failure replay](../../analysis/fixes-v8-20261028/VALIDATION.md).
The v7 authorization cache below accelerates host checks, not model generation;
the full benchmark still requires thousands of sequential source model calls.

Version 7 adds generic capability/decision history, recipient coverage, scoped
erasure, source recency, and authorization reuse within a database transaction.
See [the v7 contract and limitations](../../GOVERNED_AUTHORIZATION.md). Preserve
old runs and start a fresh versioned directory:

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 9 \
  --run-dir runs/gatemem-medical-raw-stream-v9
```

`pipeline_seconds.evolution_total` includes neighbor search; the older evolution
attempt timer does not. Permission `processing_seconds` includes host validation
and publication, while `model_seconds` measures observed structured model calls.
The independent judge now reports per-requirement utility coverage and reviews
flagged context semantically, keeping the original regex flags as auxiliary data.

This runbook now targets the implemented runtime in `agentic_memory/privacy/`.
It does not launch, import, or execute the old Auto Research online system.
GateMem supplies the benchmark data and native scorer, not the memory backend.

The default permission mode is now `inference`: the general LLM updater processes
each authenticated source event before questions can access that prefix. See
[the permission inference guide](../../PERMISSION_INFERENCE.md) for the bootstrap,
structured contract, pending/recovery behavior and comparison commands. Use
`--permission-mode baseline` in a separate run directory to retain the original
metadata-assisted oracle compiler. Its results are not raw-stream benchmark
results. `--permission-version` records an explicit inference version;
`--no-evolution` does not disable permission processing. Real-model inference
quality must be evaluated separately from offline execution tests.

For the actual run, keep the default `--permission-mode inference`. The agent
receives only the current raw turn text, speaker ID and event/time envelope.
`entities` (including principals, relationships and access levels), role annotations,
explicit subject hints, future turns and evaluation labels never enter ingestion.
Speakers are discovered as their turns arrive; initial permission receipts are
empty. Raw dialogue/event consent remains evidence. The evaluator retains the
original files for scheduling and scoring. Manifests and summaries record
`agent_input_contract: raw-stream-v1`. Start a fresh run directory: previous
metadata-assisted stores and predictions cannot be reused for this protocol.

## Start in terminal

The installed Python environments and cached model snapshots used below already
exist on this host. They supply dependencies only. All memory and runner source
code comes from `/home/thaddus/a-mem-variant`.

```bash
cd /home/thaddus/a-mem-variant

# Start these only if their ports are not already serving the named models.
tmux new-session -d -s amem-model -c /home/thaddus/a-mem-variant 'bash scripts/serve_model.sh'
tmux new-session -d -s amem-judge -c /home/thaddus/a-mem-variant 'bash scripts/serve_judge.sh'

# Inspect startup logs; allow initialization to finish before preflight.
tail -f runs/model-server.log runs/judge-server.log
```

The permission model uses its native 262,144-token context by default. In v8 the raw
source prefix and evidence ledger grew during an episode; the former 32,768-token
server limit rejected turn 23 with HTTP 400 when combined with the 8,192-token
output allowance. Version 9 selects a smaller model context with optional targeted
retrieval; relevant control evidence and global restrictions can still grow.
`AMEM_MAX_MODEL_LEN` can override the serving limit. Changing
this shell variable requires restarting the model server; it never truncates the
source history or supplies evaluation metadata. Check the loaded model's native
context and available KV cache before increasing it for another model.

The main server explicitly uses the `guidance` structured-output backend. With
the installed vLLM 0.26.0 / XGrammar 0.2.3 stack, v8 source t061's schema caused a
native XGrammar fatal error (`earley_parser.cc:209`, unsupported element type 5)
and terminated the server. The client's later `transport URLError` was a symptom;
retrying a dead service cannot recover it. Preserve the server crash log, restart
with `bash scripts/serve_model.sh`, wait for `/health`, then resume the existing
run with its exact configuration. Do not restart ingestion from an empty store.
The [recovery diagnostic](../../analysis/transport-recovery-v8-20261028/VALIDATION.md)
records exact-request checks and the serving change separately from the immutable
v8 manifest. Serving backend changes can affect generations even with unchanged
weights, prompts, schema and temperature.

The model launcher also loads `scripts/vllm_schema_support/sitecustomize.py` in
the API process and engine workers. It shares repeated JSON subschemas through
local `$defs`/`$ref` before Guidance compilation, preserving every constraint and
property/enum order. Without this hook, v8 source t078 received HTTP 400 with
`Grammar error: schema too large`. This is a compiler limit, not a context-window
failure. The evaluation still submits and hashes its original schema; its pinned
code and saved predictions remain unchanged. The exact-request regression and
recovery checks are recorded in the
[t078 diagnostic](../../analysis/http400-recovery-v8-20261028/VALIDATION.md).

After both logs report application startup complete, stop `tail` with Ctrl+C:

```bash
tmux new-session -s amem-eval -c /home/thaddus/a-mem-variant
```

Inside that session:

```bash
bash scripts/run_gatemem.sh --permission-mode inference --run-dir runs/gatemem-medical-raw-stream
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
bash scripts/run_gatemem.sh --permission-mode inference --run-dir runs/gatemem-medical-raw-stream --resume
```

Plain resume rejects changed source data, engine/adapter/policy code, processor IDs
or causal run settings. Start a new directory for a fixed-implementation comparison.
For an explicitly chosen repair continuation, `--resume-code-revision PREVIOUS_DIGEST`
acknowledges the exact previously active implementation and appends an audited code
revision. It cannot bypass dataset, model or causal configuration checks. The
original manifest, predictions and scores stay immutable; `effective_manifest.json`
and `implementation_revisions.json` describe the revised code, and the summary sets
`mixed_implementation: true`. Later plain `--resume` uses that active revision.
A model-request retry may repeat an external computation after a crash, but
committed source membership and official predictions are idempotent. Run locks
prevent concurrent writers.

Transient model transport failures in permission inference, answering and judging
wait and retry at the current source/query with 5–30 second backoff. The runner
writes `recovery.json` and an append-only `recovery-events.jsonl` and logs the stage,
reason and delay. Pending sources recover at their original observed timestamps
before earlier completed turns are replayed. No source is skipped, no permission
check is relaxed, and no checkpoint is scored until its required stage succeeds.
Persistent invalid responses stop after the durable repair budget; an unavailable
service can remain visibly waiting. Permanent request/configuration failures stop.
Ctrl+C remains immediate. Unexpected programming/configuration errors and access
denials still stop rather than being swallowed by recovery.

Recipient coverage remains mandatory, with live evidence and one entry for every
required principal. The host derives its diagnostic change labels from validated
recipient activity, assignment and restriction changes. A subject authorizing a
helper is not automatically a change to the subject's own access. Label adjustments
and the original model coverage are retained in permission attempt validation;
they never create grants or repair invalid operations. See the
[t114 regression and recovery](../../analysis/resilient-recovery-v8-20261029/VALIDATION.md).

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

Answer generation uses a structured schema whose citation enum contains only the
exact evidence IDs supplied for that question. A source-turn ID cannot replace a
field projection such as `:contact`. The local validator independently checks
citations, and the release barrier still rechecks every consumed input regardless
of which notes the answer cites. A citation mismatch indicates an invalid model
response; it does not by itself establish unauthorized memory access.

## Complete incremental protocol

The pinned medical data contains 21 episodes, 4,294 turns and 579 original queries.
There are 549 distinct query boundaries and 18 data-only tail boundaries: **567
boundaries total**. Every boundary introduces at least one new source turn.
`boundary_manifest.jsonl` and `schedule_summary.json` record the reviewed schedule.
The complete manifest belongs only to the evaluator; it is never model context.

At each boundary:

1. Take only the new turn slice since that episode's prior committed boundary.
2. Apply verified control events and ingest protected observations/notes.
3. Embed fields locally and queue optional governed semantic evolution.
4. Commit the boundary's canonical source membership and access state.
5. Ask every original query at that boundary against the same memory state.
6. Record all first-attempt predictions before invoking the independent scorer.
7. Drain at most one optional enrichment batch, commit scores and move to the next boundary.

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
- `memory.sqlite`: source/memory resources, semantic edges, dependencies, revisions,
  provenance, tombstones and memory vectors.
- `memory.privacy.sqlite`: authoritative policies, groups, assignments, directed
  access bindings, authority provenance and privacy discovery vectors. Back up and
  restore both files together with writers stopped; neither file uses WAL.
- `evaluation.sqlite`: durable boundary state and immutable first predictions.
- `official_predictions.jsonl`: original predictions with the actual exposed
  evidence and dependency IDs, in evaluation-only storage.
- `scores.jsonl`: native auxiliary and separate model judgments.
- `boundary_results.json`: per-boundary ingestion/query counts and elapsed time.
- `summary.json`: exact coverage, U/A/F/OR/MGS, action accuracy, and end-to-end
  leakage metrics that count forbidden model-context exposure as well as answers.

`complete=true` requires all 579 original queries, all 567 boundaries, all 4,294
source turns, exact ID sets, and a score for every query. Zero-value rates for
unobserved categories are replaced by nulls. Known model transport/schema errors
block the current stage and retry with visible recovery status, rather than
counting missing evaluations as passes.
The local judge uses constrained JSON with a category-specific boolean verdict,
plus independent schema validation and at most three schema attempts. Invalid or
missing verdicts never become passing scores. If a repair changes pinned code,
use a fresh experiment or the explicitly audited mixed-implementation continuation
described above.

With an LLM judge, `privacy_leak` and `deletion_leak` (and their answer-level
aggregate rates) use the judge's verdict directly. The judge evaluates disclosure
or confirmation relative to what the question already supplied, across both
`answer` and all nested `answer_structured` fields. A generic phrase or an
unconfirmed repetition in a refusal is insufficient evidence of disclosure.
Affirming a guessed value or linking it to a person can still leak without a
literal pattern match. Action correctness remains a separate check.

Keyword/regex answer matches remain in each score's `auxiliary` object for
diagnostics; they cannot override the judge. Context-exposure checks remain
rule-based and end-to-end leakage is the judge's answer verdict OR context
exposure. Offline runs without a judge are explicitly auxiliary-only and do not
measure judged disclosure. The judging policy lives in `scoring.py` so it is
included in the run's implementation fingerprint. Independent re-judging also
pins this file and requires a fresh output directory if it changes.

Evolution allows three contract-rejected proposals per job (the core accepts a
limit of 1–5). Transport failures have separate audited attempts and do not consume
this budget. Validation errors receive a field path and correction instruction,
including after a resumed outage. Access denials remain errors. Exhausted model
proposals leave source notes intact and mark the job
`rejected`; the boundary can still complete. Finished jobs are idempotent.
`summary.json.evolution` reports applications, repairs, rejections and attempts.

Context must quote its target source note; tags and keywords may describe it
using semantic retrieval labels. These affect retrieval vectors; the answer model receives the original source text,
not a generated description. Time roles, uncertainty and revision sequence must
come from source evidence. Edges are bounded `related` retrieval hints and never
grant access. All generated resources retain full-input dependency restrictions.
When a neighbor is selected through a generated overlay, its canonical source is
the factual input but the overlay remains a dependency, preserving its narrower
audience. Previous generated descriptions never become new source facts.

`evolution_jobs` and `evolution_attempts` in the memory database retain status,
error codes, hashes and timings. Raw proposals, validation paths and the prompt
are stored in unindexed `evolution_diagnostic` resources governed by all consumed
inputs. Normal dependency deletion clears these payloads too. Operator inspection
of a final rejection can join attempts to resources using `diagnostic_id`; these
records are not answer evidence.

Run a paired comparison in fresh stores, including the boundary-28 case:

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -u -m evaluation.gatemem_medical.evolution_ablation \
  --episode-limit 2 --judge-provider openrouter --output-root runs/NEW-evolution-ablation
```

This uses the configured local answer model and OpenRouter judge. Use
`--judge-provider local` when the local judge is running. Both arms share the
same code, data, embedding and model fingerprints; only evolution differs.
`--resume` continues interrupted arms with the pinned implementation.
`comparison.json` contains paired utility wins/losses, answer and context leakage,
repair outcomes, boundary-28 completion and sample-size limitations.
`paired_queries.jsonl` identifies changed answers and regressions. The subset has
56 queries, 52 boundaries and 431 source turns; coverage refers to that subset.

View progress in another terminal:

```bash
cd /home/thaddus/a-mem-variant
cat runs/gatemem-medical-full/summary.json
```

## Independent OpenRouter re-judge

To preserve the original experiment, re-judging writes a separate sibling
directory and never edits the source run's immutable predictions or scores. Add
your OpenRouter key to the private `/home/thaddus/a-mem-variant/.env` file (not this
runbook and not a tracked file):

```dotenv
AMEM_OPENROUTER_API_KEY=your_openrouter_key_here
AMEM_OPENROUTER_JUDGE_MODEL=openai/gpt-4.1
```

Then validate the credentials with one small request and run the complete
re-judge. The model receives the existing judge case only; it does not rerun the
memory system or regenerate any prediction.

```bash
cd /home/thaddus/a-mem-variant
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
the core. Missing assignments deny access. The explicit fixture workflow convention can
compile authenticated task performance into narrow source-provenanced field
assignments; roles, authorship, visibility claims and queries alone grant nothing.
Natural-language consent/deletion interpretation is bounded and conservative;
unsupported language can cause excessive denial or benchmark errors. This is
measured openly, not fixed by exposing hidden scoring annotations. The family
logistics projection uses complete permitted source clauses under a separate
authoritative field policy, retaining locations and cancellation wording when
approved. Mixed clinical clauses can remain unavailable. It is not an LLM summary.
See [the authority policy](AUTHORITY_POLICY.md) for interpretation boundaries and
[the two-store migration guide](../../PRIVACY_TWO_STORE.md) before opening old data.

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
