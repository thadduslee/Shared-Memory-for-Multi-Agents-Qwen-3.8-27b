# shm_evolve

A governed shared memory for multi-agent episodes, plus an optimiser that
evolves it. Full design and decision log: [`DESIGN.md`](DESIGN.md) (read
that first for anything this file only summarises).

## What this is

There are two loops. The **inner loop** (`memory_system/`) is a shared
memory that reorganises itself *within* one episode: a fixed pipeline --
front door &rarr; queue &rarr; processor &rarr; librarian &rarr; store, with
a retriever at query time -- decides, turn by turn, what to keep, who may see
it, and how the store's own structure should change (new fields, new
collections, new views) to fit what is being said. It implements the
benchmark interface GateMem expects (`GateMemAgent.reset/ingest/query`) and
runs with or without a model: every component has a deterministic fallback,
so the full test suite and an end-to-end run both work with no endpoint. The
**outer loop** (`loop/`) evolves that memory across episodes: it evaluates
the current ("champion") prompts, judges the result against GateMem's own
scorer, attributes each failure to the specific component and decision that
caused it, has an LLM reflect on the attribution and propose a diagnosis, has
a second LLM turn that diagnosis into a small bullet-level edit of one
component's prompt, and gates the edit on a paired win/loss probe before it
is allowed to become the new champion.

What's frozen and what evolves is deliberately narrow. Frozen: the pipeline
shape itself, the envelope (the columns every stored record has), the closed
vocabularies of operations a component may emit (`memory_system/ops.py`),
the access gate (`memory_system/keyring.py`) and the budgeted model call
(`memory_system/llm.py`). Evolvable: exactly four prompt files, one per
inner-loop component (`memory_system/prompts/{front_door,processor,
librarian,retriever}.md`), one bullet-level edit at a time, one component per
outer-loop iteration. The reflector and curator prompts
(`loop/prompts/{reflector,curator}.md`) are the optimiser's own prompts, not
part of the system being optimised, and are not themselves evolved.

This replaces `Auto Research/`, the previous LangGraph-based loop, whose
Developer node *wrote code* -- every iteration could produce a different
memory implementation, so a regression could come from anywhere, and the
benchmark's own harness had to re-review a diff before trusting it. Here the
scaffold never changes: only four prompt files do, in small bounded edits, so
a regression is attributable to one component's prompt and one diagnosis,
and the paired probe gate is the only thing standing between a candidate
edit and the next champion. `shm_evolve` takes `gsm-rebuild/`'s ideas
(envelope, supersession, projections, reason codes) but not its Postgres
service -- everything here is `sqlite3` plus `httpx`, no external service to
run.

## Quickstart (offline, no endpoint)

```bash
cd shm_evolve
pip install -e .[dev]
python -m pytest -q                        # 180+ tests, no model, no GPU
python -m loop.run --domain office --iterations 2 --fake-llm --episodes 2
```

`--fake-llm` installs a deterministic stand-in for every model call (memory,
answerer, judge, reflector, curator) that still exercises the real parse /
validate / apply / trace code paths -- see `memory_system/fake_llm.py` and
`loop/fake_llm.py`. `SHM_CONTRACT_MODE=offline python -m pytest -q
tests/test_contract.py` runs the ported contract with the model fully
disabled (every component's hand-written fallback decides); the default runs
it in both modes.

## A real run

```bash
set -a; source ../.env; set +a                 # OPENROUTER_API_KEY etc.
source ../scripts/serve/env_local_eval.sh       # VLLM_BASE_URL, JUDGE_*, GATEMEM_REPO, ...
cd shm_evolve
python -m loop.run --domain office --iterations 20
```

That script is shared with `Auto Research/`'s config, so one shell session
drives both loops with the same endpoint names
(`VLLM_BASE_URL`/`EVALUATOR_MODEL`/`JUDGE_*`/`OPENROUTER_*`). Env vars that
matter specifically here, on top of it:

* `MEMORY_LLM_TOKEN_BUDGET` / `MEMORY_LLM_CALL_BUDGET` (default
  `1_500_000` / `900`) -- the per-episode budget for the memory's own model
  calls (`memory_system/llm.py`). Measured on a real 240-turn office
  episode: ~550 calls, ~1.07M prompt tokens for one pass; these defaults
  leave headroom above that. If `eval_report.json`'s one-line log shows
  `budget-refused=N` for N much above zero, most of that episode ran on
  fallbacks, not the model -- raise the budget, not just a concern.
* `MEMORY_LLM_BASE_URL` / `MEMORY_LLM_MODEL` default to the evaluator's own
  `VLLM_BASE_URL` / `EVALUATOR_MODEL` -- the memory and the answerer share
  one endpoint unless you point the memory elsewhere.
* `EPISODE_WORKERS` (default 8) -- how many episodes run concurrently, one
  process each. **Recommend ~4** against a single local GPU: each episode
  process calls the memory model directly via plain blocking HTTP
  (`memory_system/llm.py`, frozen), with NO concurrency limiter shared across
  processes -- unlike the answerer's calls, which all share one semaphore in
  the parent (`loop/config.py`'s route `max_concurrency`). At the default of
  8, up to 8 processes' memory calls and the parent's answerer fan-out all
  contend for the same GPU with nothing coordinating them; 4 leaves the
  answerer's `VLLM_MAX_CONCURRENCY` room to actually matter.
* `MIN_WIN_MARGIN` (default 0, a plain sign test) -- **recommend 2** when the
  answerer thinks (`EVALUATOR_THINKING=true`, the default -- see
  `loop/answerer.py`'s "WHY THINKING IS ON"). DESIGN.md §3.5 already notes
  U/A/F run-to-run noise of roughly ±0.03/±0.06 at probe size; a thinking
  answerer adds its own run-to-run variance on top of that (longer, more
  variable completions, occasional truncation), so a margin of 0 is more
  likely to accept a win that is really noise. 2 is a judgment call, not a
  measured constant -- there is no experiment behind the exact number, only
  the documented noise figures and the fact that thinking makes it worse.
* `EPISODE_TIMEOUT_S` (default 3600) -- per-episode wall-clock bound; a hung
  child is recorded as a `timeout` error and does not stall the rest of the
  evaluation (`loop/evaluate.py`).
* `GATEMEM_REPO` -- the GateMem checkout `bench/` lives under (prompts,
  data, the scorer). Defaults to this repo's own root, which is correct here
  since `bench/` is `shm_evolve/`'s sibling; set it only if your GateMem
  checkout is elsewhere.
* `QUEUE_BATCH` -- leave at the default (1). See DESIGN.md §6: above 1, a
  query can end up running the processor (and `keyring.grant`) itself, which
  only the default is tested against.

## Artefacts: `runs/<run>/`

```
runs/<run>/
  run.json              pinned at creation: domains, agent path, eval episode ids,
                         probe seed -- a resumed run cannot silently change what it measures
  history.jsonl         one row per iteration (append-only): accepted?, component,
                         mechanism, scores, wins/losses, elapsed time
  evals.json            release number -> path of its cached full evaluation
                         (an accepted candidate's full eval becomes the next champion's)
  judge_cache.jsonl      verdicts cached by (checkpoint, output, use_llm), run-wide
  probe_set.json         the seeded probe episodes plus any promoted failing checkpoints
  releases/
    HEAD                 the current champion's release number (plain text)
    <n>/                 prompts/<component>.md (all four), delta.json, probe.json,
                         scores.json, meta.json -- append-only, never rewritten
  rejected/
    <iteration>/         same shape as a release, for a candidate the reflector,
                         the curator's lint, or the gate turned down
  iter_<n>/
    champion_eval/        this iteration's evaluation of the champion (reused
                         across iterations via evals.json until the champion changes)
    champion_probe/       probe-set evaluation of the champion
    candidate_prompts/    the four prompt files with this iteration's edit applied
    candidate_probe/      probe-set evaluation of the candidate
    candidate_full/       full evaluation of the candidate (only on ACCEPT)
    reflector/, curator/  each stage's own input/output dump
    gate.json             the paired win/loss decision
  <eval_dir>/episodes/<episode_id>/
    memory.db, predictions.jsonl, traces.jsonl, result.json
  <eval_dir>/
    checkpoints.stripped.jsonl   the ONLY checkpoint file an episode process may read
    predictions.jsonl, traces.jsonl, eval_report.json
    verdicts.jsonl, scores.json  (written by judge())
    attribution.json, failures.jsonl   (written by write_attribution())
```

### Reading `attribution.json` and `failures.jsonl`

`failures.jsonl` has one row per failed checkpoint: `query_type`,
`asker_role`, the `verdict`, the released/denied record ids, the trace
`chain` that produced them (in `seq` order), and an `attributed_component` /
`attributed_op` -- the ONE decision the reflector should look at first (the
rule that picks it is `loop/attribution.py`'s module docstring, case by
case). `note` is a one-line human reason. `attribution.json` aggregates: failure
counts by `(component, op, outcome)` and by query type, a denial-saturation
table per requester role (a `saturated` group fails almost every utility
check with nothing released -- a structural policy hole, not noise), and
`prompt_rejections` (how often each component's model reply was rejected by
the scaffold, independent of score). The one-line log per evaluation
(`loop/run.py`) already surfaces the top few `(component, op, outcome)`
counts and any saturated groups.

## The four prompt files

`memory_system/prompts/front_door.md`, `processor.md`, `librarian.md`,
`retriever.md` -- one per evolvable component (DESIGN.md §2.2-2.5), seeded by
`SHM_SEED_PROMPTS_DIR` (default: those same files) when a run starts from
nothing. **To seed a new run from a previous release**, point
`--seed-prompts` (or `SHM_SEED_PROMPTS_DIR`) at that release's own prompts
directory:

```bash
python -m loop.run --domain office --run-id my-new-run \
    --seed-prompts path/to/old/runs/<old-run>/releases/<n>/prompts
```

This only takes effect for a run directory that has no release chain yet
(`ReleaseChain.exists()`); resuming an existing run always continues from its
own `releases/HEAD`, never from `--seed-prompts`.

## Invariants (DESIGN.md §2.8), as a checklist

Each of these is a property the TESTS enforce, not merely an aspiration:

- [ ] Every text in `evidence` was opened with the asker's own identity
  (`tests/test_contract.py::test_released_texts_were_opened_with_the_requesters_key_granted_before_the_query`).
- [ ] `keyring.grant` is never called during `query`
  (`tests/test_invariants.py::test_grant_is_never_called_during_query`; only
  guaranteed at the default `QUEUE_BATCH=1` -- see DESIGN.md §6).
- [ ] A `grant` without a valid `authority` is rejected
  (`tests/test_invariants.py::test_grant_without_authority_is_rejected`, and
  the authority must itself be able to see the record).
- [ ] No structure op can add, drop, rename or alter an envelope column
  (`tests/test_invariants.py::test_structure_ops_cannot_touch_envelope_columns`).
- [ ] Every view includes `tombstoned_at IS NULL`
  (`tests/test_invariants.py::test_views_always_include_the_tombstone_filter`).
- [ ] After `shred`, no file under the db directory holds the plaintext
  (`tests/test_invariants.py::test_after_shred_no_file_holds_the_plaintext`;
  see the sqlite_master caveat under Known limitations).
- [ ] `query` leaves the database byte-identical
  (`tests/test_invariants.py::test_query_leaves_the_database_byte_identical`;
  likewise only guaranteed with the queue already drained, the default
  case).
- [ ] No dataset vocabulary in `memory_system/` or prompts
  (`tests/test_vocabulary_lint.py`, the arbiter for this rule).
- [ ] The ported `tests/test_contract.py` passes against the real agent, in
  both model modes.

## Known limitations

- The offline/fake deterministic fallbacks are noticeably weaker on the
  medical domain than on office: more of its instructions are phrased in
  ways the generic structural/cue-based fallback cannot resolve precisely,
  so a real model matters more there than elsewhere.
- Fake mode's front door classifies by keyword (`memory_system/fake_llm.py`
  and the shared cues in `memory_system/cues.py`), which can read two
  unrelated statements as contradicting each other more often than a real
  model would -- expect more `integrity:contradiction` signal noise under
  `--fake-llm` than with a model.
- A `create_view` predicate embeds its literal values directly in the view's
  SQL text (`memory_system/envelope.py:create_view`), which is stored in
  `sqlite_master`. If a literal happens to come from sensitive content, it
  can persist there even after the record it came from is shredded -- there
  is no `drop_view` op to remove it. Not currently covered by the
  after-shred invariant test, which only checks record bodies.
- The deterministic fallback's name/entity matching (front door's
  `structural_audience`, the processor's content-overlap floor in
  `memory_system/processor.py`) is loose: it can over- or under-match a
  neighbour that shares words with, but is not, the thing a chunk names. The
  strong-key check added for deletions (dash/underscore-joined tokens, mixed
  alnum ids) narrows this for token-like secrets specifically; free-text
  references (a name, a mapping stated only in prose) still fall through to
  the looser heuristic.
- The vocabulary lint (`tests/test_vocabulary_lint.py`) collects every role,
  relationship type and id from the benchmark's own data and forbids them
  anywhere in `memory_system/` or its prompts. When a role happens to be an
  ordinary English word, the lint can flag a wholly unrelated, legitimate
  use of that word -- strictness traded for not having to hand-maintain the
  list.
- Memory-model calls made by episode child processes go straight to
  `MEMORY_LLM_BASE_URL` over plain blocking HTTP (`memory_system/llm.py`,
  frozen) and are not subject to the parent's per-route concurrency
  semaphore (`loop/config.py`/`loop/llm_client.py`) that bounds the
  answerer's and judge's fan-out. `EPISODE_WORKERS` is the only real lever
  on how much load this adds to a shared endpoint; see the real-run section
  above.
