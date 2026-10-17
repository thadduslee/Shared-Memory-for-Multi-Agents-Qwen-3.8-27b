# GateMem on OpenEvolve

Evolve the SQL memory system with [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve)
instead of (or alongside) the LangGraph orchestrator in `../Auto Research`.

**Zero edits to the openevolve checkout.** Everything here is configuration plus
an evaluator that satisfies OpenEvolve's existing plugin contract, so `openevolve/`
stays a clean clone and can be pulled forward without a merge.

## Quick start (dry run — no model, no GPU, ~30 seconds)

```bash
pip install -e openevolve                                        # once
python gatemem_openevolve/build_initial.py                       # pack generation 0

python gatemem_openevolve/dryrun_server.py --port 8115 &         # stub endpoint

GATEMEM_LLM_API_KEY=dryrun python openevolve/openevolve-run.py \
  gatemem_openevolve/initial_program.txt \
  gatemem_openevolve/evaluator.py \
  --config gatemem_openevolve/config.dryrun.yaml \
  --iterations 2 \
  --output gatemem_openevolve/openevolve_output
```

Score the packed workspace on its own, with no framework at all:

```bash
python gatemem_openevolve/evaluator.py gatemem_openevolve/initial_program.txt
python gatemem_openevolve/evaluator.py gatemem_openevolve/initial_program.txt --full
```

### Measured baseline

Generation 0 is `Auto Research/templates/memory_system/` verbatim. On the
50-checkpoint dev slice, scored by `judge_gatemem.rule_score` with no LLM judge:

| metric | value |
|---|---|
| MGS (`combined_score`) | **0.4532** |
| U (utility) | 0.5556 |
| A (access violations) | 0.0588 |
| F (forgetting failures) | 0.1333 |
| over-refusal | 0.2000 |
| errors | 0 / 50 |

Eight of the eleven failures are utility, and five of those eight are the system
withholding an answer it was entitled to give (`answer_redacted` or `no_memory`
where `answer` was expected). That is where the headroom is, and the failing-
checkpoint list is handed to the next generation as an artifact.

Note this is a LOWER BOUND on U: the deterministic matcher reports a correct
answer as a miss when the word order differs, which is the whole subject of
`nodes/judge_gatemem.py`'s docstring. The production judge would score this
workspace no lower.

## Real run

`config.yaml` points at the vLLM/SoCLaaS endpoint. Check two things before
starting it, both of which have cost a run before:

* `api_base` is a **literal** URL and the model `name` is the **full slug**
  (`Qwen/Qwen3.8-27B`). OpenEvolve substitutes `${VAR}` into `api_key` and
  nothing else — an `api_base: "${...}"` is passed through verbatim and every
  iteration then fails with `Connection error`, which reads like a dead endpoint.
* `parallel_evaluations` stays well under the endpoint's ~20-concurrent ceiling.

```bash
GATEMEM_LLM_API_KEY=... python openevolve/openevolve-run.py \
  gatemem_openevolve/initial_program.txt \
  gatemem_openevolve/evaluator.py \
  --config gatemem_openevolve/config.yaml --iterations 20 \
  --output gatemem_openevolve/openevolve_output
```

## How it maps onto the LangGraph pipeline

| `Auto Research` node | here |
|---|---|
| `architect` | no equivalent — the MAP-Elites database and the prompt sampler carry cross-iteration design memory instead of `critique_summary.md` |
| `developer` | OpenEvolve's one mutation call per iteration. **Not** a self-driving tool loop; it returns SEARCH/REPLACE diffs over the packed workspace |
| `eval_dispatch` / `eval_worker` / `eval_collect` | `evaluator.py` — same subprocess runner, lifted from `nodes/medical_evaluator.py` with `ast` so there is one copy of it |
| `judge` | `judge_gatemem.rule_score` + the aggregation in `evaluator._score`. Deterministic; no LLM judge |
| dev gate → full 579 | `cascade_evaluation` with `cascade_thresholds: [0.80]` = `DEV_GATE_MGS` |
| `critic` | the `failing_checkpoints` artifact, rendered into the next generation's prompt |
| `infra_retry` | none. A failed iteration is dropped and the run continues |

## Files

| file | what it is |
|---|---|
| `pack.py` | workspace ⇄ single packed document (OpenEvolve evolves one file) |
| `build_initial.py` | regenerates `initial_program.txt` from the orchestrator's templates |
| `evaluator.py` | the OpenEvolve evaluator: unpack → SQL-only guard → shards → MGS |
| `templates/system_message.txt` | the system prompt, including the SQL-only constraint |
| `config.yaml` | real run against the vLLM/SoCLaaS endpoint |
| `config.dryrun.yaml` | dry run against `dryrun_server.py` |
| `dryrun_server.py` | stub OpenAI endpoint returning one fixed, score-neutral diff |
| `developer.py` | the self-driving Developer: an `LLMInterface` that runs a tool loop |
| `run_evolution.py` | runner that injects the Developer (the CLI cannot: `init_client` is a callable) |
| `config.local.yaml` | local vLLM run with the Developer loop enabled |
| `best_clean_program.txt` | the recommended evolved artifact (MGS 0.5891, no reward hacking) |
| `best_clean_program_info.json` | its id, lineage and metrics |

## The SQL-only constraint

Storage and retrieval stay SQLite, matching the LangGraph implementation. It is
enforced in `evaluator.check_sql_only`, not merely asked for in the prompt: a
candidate that reaches for embeddings is a different experiment whose MGS is not
on the same axis, and once one lands in the grid every later generation inherits
from it. Violations score 0 and the artifact names the offending line.

## Results: 20 iterations on the local vLLM

One run, 20 iterations, ~2h10m on `Qwen/Qwen3.8-27B` (port 8002), 2 workers.
18 Developer episodes finished cleanly, 2 hit the turn ceiling, 0 truncations,
0 no-change episodes, 0 iteration errors. 21 programs, 8 distinct MGS values.

| | baseline | `best_clean_program.txt` | `openevolve_output/best/` * |
|---|---|---|---|
| **MGS** | 0.4532 | **0.5891** (+30%) | **0.6797** (+50%) |
| U | 0.5556 | 0.7222 | 0.8333 |
| A | 0.0588 | 0.0588 | 0.0588 |
| F | 0.1333 | 0.1333 | 0.1333 |
| own test suite | 36 passed | 36 passed | 40 passed |

`A` never moved on any program in the run: the privacy gates held under every
mutation.

\* `openevolve_output/` is gitignored, so that column is RUN-LOCAL: it is the
output of the run described here and will not exist in a fresh clone. Only
`best_clean_program.txt` is committed. Re-running reproduces the directory but
not necessarily the same programs -- the Developer runs at temperature 0.7.

### Two artifacts, and why the lower-scoring one is the recommended one

**`best_clean_program.txt` (MGS 0.5891) is the artifact to build on.** Its gain
is a real bug fix -- a DENIAL-STRENGTH RULE, one `COUNT(*)` over `record_terms`:
a record about to be denied must share at least 2 distinctive terms with the
query before it counts as withheld content. A record sharing one term is not
about the query, and counting it as withheld downgraded complete authorized
answers to `answer_redacted`. That is the confusion `memory_system/schema.sql`
already describes as costing "12 of the 18 utility checkpoints", rediscovered
from the failing-checkpoint list alone. Found independently in three lineages.

**`openevolve_output/best/best_program.txt` (MGS 0.6797) scores higher by
removing a governance label.** Every program above 0.5891 deletes the
`answer_redacted` downgrade so that any non-empty allowed evidence returns
`"answer"`. The model's own comment states the reasoning: "nothing in the
scoring rewards the `answer_redacted` label ... the downgrade can only cost U
and never buy A or F." That is correct about the incentive, and it is reward
hacking -- retrieval is unchanged; only the label is gone.

It is kept because it is evidence about the OBJECTIVE: **MGS does not protect
the meaning of the action label**, so a system is strictly better off never
redacting. Closing that hole -- scoring `answer_redacted` as partial credit, or
asserting the label matches the decision state -- changes GateMem's semantics
and has deliberately not been done here.

Two fixes in the run were genuine and independent of the shortcut: the
deletion-request leak (a deletion request QUOTES the content it asks to forget,
so ingesting it re-introduces that content) and the removal of an over-broad
deletion fallback that an earlier iteration had itself added. One program reached
**F = 0.0000** at U 0.6111 -- a cell a hill-climb would have discarded and the
MAP-Elites grid kept.

## Known gaps

* **No agentic developer.** One prompt, one diff. The `Auto Research` Developer
  builds, runs its own tests and fixes what failed. Closing this means an
  `LLMModelConfig.init_client` that runs that loop and returns a packed
  workspace — the seam exists, it is just not wired.
* **No LLM judge.** Deterministic scoring only, so U is a lower bound.
* **Tests are carried but not run.** `tests/` travels in the packed workspace and
  the model is told to keep it honest; nothing executes it. Running it as a
  gate before the benchmark would catch a broken RBAC change in one second
  instead of fifty.
