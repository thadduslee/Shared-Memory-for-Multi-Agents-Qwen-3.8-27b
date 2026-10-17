# shm_evolve — design

A governed shared memory that **reorganises itself within an episode** (the
inner loop), and an optimiser that **evolves the prompts of the components
that do the reorganising** across episodes (the outer loop). Replaces
`Auto Research/` (the LangGraph loop, whose Developer wrote code) and takes
from `gsm-rebuild/` only its ideas (envelope, supersession, projections,
reason codes), not its Postgres service.

Decisions already made (do not reopen):

| Decision | Choice |
|---|---|
| Pipeline | fixed scaffold: front door → queue → processor → librarian → store; retriever at query time |
| What evolves | **prompts only**, one component per outer iteration |
| Structure changes | librarian emits actions from a **closed DDL vocabulary**; the envelope is frozen |
| Inner-loop signals | **1** instructions in the stream, **2** write-time integrity signals. Read-time signals (3) are *logged, never acted on* |
| Access rules | policy *facts* are data; the *gate* is frozen (`keyring.py`); only *interpretation* evolves |
| Outer-loop optimiser | Reflector → Curator (ACE-style deltas), paired win/loss probe gate, append-only release chain |
| Infra | plain Python 3.12+: `sqlite3`, `httpx`, `pytest`. No LangGraph, no dsh |
| Testability | everything runs with a deterministic fake LLM; no GPU is required for any test |

## 1. Vocabulary

* **principal** — a person or agent with an id and a role (from `episode["entities"]["principals"]`).
* **chunk** — one unit of incoming information: a turn's text, or a piece of it after the front door splits it.
* **record** — one stored unit in the envelope (below). A chunk becomes one or more records.
* **audience** — the set of principal ids that hold a key for a record. "Private" is an audience of one. There is one store and one gate, not a private store and a shared store.
* **envelope** — the frozen columns every record carries, whatever else the librarian adds.
* **component** — one of `front_door`, `processor`, `librarian`, `retriever`. Each has exactly one evolvable prompt file in `memory_system/prompts/<component>.md`.

## 2. The inner loop (memory_system/)

```
turn ──► front_door ──► chunks ──┬─ tightening ops ──► librarian (applied now, never queued)
                                 └─ everything else ──► queue ──flush──► processor ──► ops ──► librarian ──► store
query ──► retriever ──► candidates ──► GATE (keyring.open_for as requester) ──► evidence + action
```

### 2.1 front_door  (prompted; evolvable)
Input: one turn (`turn_id, timestamp, turn_kind, speaker{principal_id, role}, text`) plus the episode's declarations.
Output: a list of chunks, each with: `text`, `kind` (one of the closed `ChunkKind` set in `ops.py`), `about` (principal ids the chunk concerns, may be empty), `suggested_audience` (principal ids; the processor may widen it only with authority, see §2.3), `is_tightening` (bool).

Rules the scaffold enforces regardless of the prompt:
* **Default private.** A chunk with no `suggested_audience` gets `{speaker}`.
* **Split before classify.** A mixed turn is split; the prompt decides the split, the scaffold never merges chunks back.
* **Tightening fast path.** A chunk whose kind is `revoke`, `delete`, or `restrict` bypasses the queue and goes straight to the librarian. Tightening can never be wrong in the unsafe direction, so order does not matter; widening must wait its turn.

### 2.2 queue  (not prompted)
FIFO by `(timestamp, turn_id)`. `flush()` runs the processor over everything pending and is called **before every query** and whenever `len(queue) >= QUEUE_BATCH` (config, default 1). The queue exists so the processor can see several chunks at once (dedupe, merge, contradiction across speakers); it is never allowed to make a query see stale state.

### 2.3 processor  (prompted; evolvable)
Input: a batch of chunks in order, plus a *read-only view* of current memory relevant to them (the retriever's `peek()`, see §2.5 — with the OWNER identity, since this is maintenance, not release).
Output: a list of **update ops** from the closed vocabulary in `ops.py`:

| Group | Ops | Notes |
|---|---|---|
| record | `add`, `split`, `merge`, `supersede`, `derive`, `link`, `delete`, `shred` | `supersede` keeps the old version with `valid_to` set; `delete` tombstones; `shred` is `delete` + key destruction |
| policy | `grant`, `revoke`, `restrict` (narrow granularity: full → summary → existence-only), `expire` | **`grant` requires an `authority` field naming the principal whose turn authorises it, and the scaffold checks that principal is in the source chunk's `about` ∪ `{speaker}`; otherwise the op is rejected and logged.** |
| structure | `add_field`, `add_collection`, `add_index`, `create_view`, `rekey` | executed by the librarian as DDL, see §2.4 |
| control | `hold`, `flag_conflict`, `noop` | `hold` leaves the chunk in the queue for one more flush (max `HOLD_MAX`, default 2) |

Signals the processor receives (inner-loop signal set, decided):
1. **Instructions in the stream**: the chunks themselves (consent, revocation, correction, deletion).
2. **Integrity signals from the last librarian pass**: `IntegritySignal` list in `ops.py` — `contradiction(record_a, record_b)`, `duplicate(record_a, record_b)`, `schema_mismatch(field, value)`, `orphaned_derivation(record)`, `dangling_link(record)`. The librarian computes these deterministically when it applies ops; the processor sees them on its next call and may respond with record/structure ops.

Read-time signals (empty retrieval, denials, repeated queries) are written to the trace with `signal_kind="read_time"` and are **not** fed back. This is the next-step experiment, not this one.

### 2.4 librarian  (prompted for structure; deterministic for records and policy)
Applies ops to the store. Record and policy ops are deterministic SQL; the prompt is consulted **only** for structure ops, where it decides the concrete DDL (column name/type, which collection, index columns, view definition) within the bounded vocabulary. Every DDL statement is generated by `envelope.py`, never by the model as raw SQL.

**The envelope is frozen.** Every collection (table) the librarian creates has these columns, and the scaffold refuses any structure op that touches them:

```
record_id    TEXT PRIMARY KEY        -- ulid-like, unique across collections
collection   TEXT NOT NULL           -- the table it lives in (denormalised for the trace)
body         BLOB                    -- keyring.seal(...) ciphertext; NULL after shred
speaker_id   TEXT NOT NULL           -- principal whose words these are (evidence attribution)
speaker_role TEXT NOT NULL
about        TEXT NOT NULL           -- JSON list of principal ids the record concerns
source_turn  TEXT NOT NULL
created_at   TEXT NOT NULL           -- the turn's timestamp
valid_from   TEXT NOT NULL
valid_to     TEXT                    -- NULL while current; set by supersede
superseded_by TEXT                   -- record_id
derived_from TEXT                    -- JSON list of record_ids (provenance)
granularity  TEXT NOT NULL           -- full | summary | existence
tombstoned_at TEXT                   -- deletion; the body is shredded at the same time
```
Audience is **not** a column: it is `keyring.record_keys` (who holds a wrap). `envelope.audience(conn, record_id)` reads it. A derived record's audience is the **intersection** of its sources' audiences at derivation time, and shrinks when any source's does.

Structure ops map to DDL as follows (all names are validated identifiers; the model never writes SQL):
* `add_field(collection, name, type∈{TEXT,INTEGER,REAL})` → `ALTER TABLE … ADD COLUMN`
* `add_collection(name, fields)` → `CREATE TABLE name(<envelope>, <fields>)`
* `add_index(collection, fields)` → `CREATE INDEX`
* `create_view(name, collection, where, order_by)` → `CREATE VIEW` from a small safe predicate grammar (`field op literal`, `AND`), **always** composed with `tombstoned_at IS NULL`
* `rekey(collection, field, from_record_ids)` → fills a field on existing records (a reorganisation; no DDL beyond `add_field` if needed)

A structure op is logged with `signal_kind` naming which integrity signal (if any) motivated it, so the outer loop can see *why* structure changed.

### 2.5 retriever  (prompted; evolvable)
Input: the query (`asker{principal_id, role}`, `query_text`, `as_of_turn_id`), the current schema (collections, fields, views) and the policy facts for the asker.
Steps, fixed by the scaffold:
1. `candidates()` — the model composes a search from the vocabulary in `ops.py` (`SearchSpec`: collections, field filters from the same safe predicate grammar, keyword terms, `as_of`, `current_only`), the scaffold runs it. **Only records with `created_at <= as_of` and `tombstoned_at IS NULL` are ever candidates.**
2. **GATE** — `keyring.open_for(conn, record_id, asker, body)` for each candidate. Nothing else can produce plaintext. Records that do not open are counted in `debug.retrieval.n_denied` and never shown to the model.
3. `select()` — the model ranks and chooses from the *opened* texts and picks the action from `{answer, answer_redacted, refuse, no_memory}`. Deterministic fallbacks when the model is unavailable: current-first ordering, top-k by keyword overlap, action by rule (opened>0 → answer; denied>0 and opened==0 → refuse; else no_memory).
4. `peek()` — the same path with `OWNER` identity, used only by the processor for maintenance; never reaches `evidence`.

Granularity: a record opened for the asker but held at `summary` or `existence` granularity for them (policy op `restrict`) releases its `summary` field (a derived record) or only its existence, and the action becomes `answer_redacted`.

### 2.6 agent.py — the benchmark interface (fixed)
`GateMemAgent(db_path)`, `reset(episode)`, `ingest(turn)`, `query(checkpoint)` exactly as in `Auto Research/templates_task/TASK.md`. `_visible()` strips `HIDDEN_FIELDS` and stays in the path. `query` **must not mutate state** (it flushes the queue first; flushing is idempotent and is the only write). `reset` calls `llm.new_episode()` and seeds records from `entities.relationships`: each declared relationship becomes a policy fact (holder → target) with any extra fields kept as attributes; nothing about relationship *types* is hard-coded.

### 2.7 traces — the evidence the outer loop learns from
Every component decision is appended to `trace` (a table in the same SQLite file and a JSONL mirror): `TraceEvent` in `contracts.py` — `component, turn_id, chunk_id, op, op_args, outcome(applied|rejected|fallback), reason, record_ids, llm_called, signal_kind`. At query time the retriever writes one event per checkpoint with the candidate ids, denied ids, released ids and action. The judge joins verdicts to these events by `record_id` and `checkpoint_id`, which is how a failure is attributed to the component and the specific decision that caused it.

### 2.8 Invariants (tests enforce these)
1. Every text in `evidence` was returned by `keyring.open_for` with the **asker's** id (spy test from the old contract).
2. `keyring.grant` is never called during `query`.
3. A `grant` op without a valid `authority` is rejected.
4. No structure op can add, drop, rename or alter an envelope column.
5. Views always include `tombstoned_at IS NULL`.
6. After `shred`, no file under the db directory contains the plaintext.
7. `query` leaves the database byte-identical (hash before/after, after the flush).
8. No dataset vocabulary in code or prompts: a lint test greps `memory_system/` and `prompts/` for role names, relationship types and entity ids collected from `bench/data/*/episodes.jsonl`.
9. The old `templates_task/tests/test_contract.py` passes unchanged against the new agent.

## 3. The outer loop (loop/)

```
champion prompts ──► evaluate(episodes) ──► judge ──► reflector ──► curator ──► candidate prompts
        ▲                                                                           │
        └──────────── gate: paired win/loss on the probe vs champion ◄──────────────┘
                              │ accept → release chain head advances
                              └ reject → champion unchanged, failure recorded
```

### 3.1 evaluate.py
One pass per episode: `reset`, then for each turn `ingest`, and whenever a checkpoint's `as_of_turn_id` equals the turn just ingested, `query` it. Episodes run concurrently (process pool, one SQLite file each); the LLM calls inside go through the frozen `llm.py` with the per-episode budget set by env (`MEMORY_LLM_CALL_BUDGET`, `MEMORY_LLM_TOKEN_BUDGET`). Measured on a real 240-turn office episode: ~550 memory-model calls and ~1.07M prompt tokens for one pass. Defaults are `MEMORY_LLM_TOKEN_BUDGET=1_500_000` and `MEMORY_LLM_CALL_BUDGET=900` (leaving headroom above the measured cost); the old 400k/1200, sized for the previous replay-per-checkpoint evaluator with a warm model cache, throttled most of a real one-pass episode onto fallbacks. `eval_report.json`'s `memory_llm_total.refused_over_budget` (summed across episodes) is surfaced in `run.py`'s one-line eval log (`budget-refused=N`) so a throttled run is visible without opening the report.
The **field wall**: the evaluator writes `checkpoints.stripped.jsonl` (no annotation fields) and that file is the only checkpoint source the agent process reads; a test asserts the agent process never imports the annotated file.
The **answerer** (frozen): the benchmark's own `bench/prompts/query_prompt.txt` via the Qwen endpoint, thinking **on** (to match the baselines — see memory note `office-baselines-thinking-on`), rendering `evidence` into the answer string. Port `format_memory_block`/`render_query_prompt` from `Auto Research/prompts_gatemem.py`. Output: `predictions.jsonl` in the shape `Auto Research/gatemem_adapter.make_prediction_row` produces (so `bench/`'s own scorer can re-score it), plus `traces.jsonl`.

### 3.2 judge.py
Port `rule_score` and `merge_verdicts` from `Auto Research/nodes/judge_gatemem.py` and the LLM judge with `bench/prompts/judge_prompt.txt` (not `judge_prompt_gatemem.txt`: that is what `bench/scripts/score_predictions.py` itself uses, so the judge in this loop matches the benchmark's own scorer; see `loop/judge.py`'s module docstring). Aggregate U, A, F, MGS = U·(1−A)·(1−F), over-refusal. **Attribution**: for each failed checkpoint, join to the trace: which records were released/denied, which component produced them and under what op, whether the LLM was called or a fallback ran. Produce `failures.jsonl` with one row per failed checkpoint carrying the attributed chain, and `attribution.json` with failure counts per `(component, op, outcome)` and per requester role (keep the denial-saturation table idea from `Auto Research/nodes/judge.py`).

### 3.3 reflector.py  (LLM; OpenRouter DeepSeek by default, env-configurable)
Input: the attribution, a capped sample of failure rows (with the trace chain and the relevant *current* prompt text), the score history. Output: a diagnosis in Markdown + a JSON block `{"component": <one of four>, "mechanism": str, "evidence_ids": [...], "expected_effect": {"U"|"A"|"F": "+"|"-"}}`. It must pick **one** component. The prompt gives no domain words.

### 3.4 curator.py  (LLM)
Input: the diagnosis and the chosen component's current prompt. Output: a **delta** — a JSON list of edits `{"op": "add"|"replace"|"remove", "anchor": <existing line or null>, "text": str}` applied to the prompt's bullet structure, capped at `MAX_EDITS` (default 3) and `MAX_PROMPT_CHARS`. Never a full rewrite. The resulting prompt is linted (no dataset vocabulary; parses; under size).

### 3.5 gate.py
* **Probe**: a fixed sample of `PROBE_EPISODES` (default 3 per domain, seeded) run for both champion and candidate. Score per *checkpoint*, pair by `checkpoint_id`, count wins/losses/ties on the merged verdict; accept iff `wins − losses > MIN_WIN_MARGIN` (default 0, i.e. a sign test). This replaces the mean-minus-tolerance rule because U/A/F noise is ±0.03/±0.06.
* On accept: run the full set (or the configured slice), write the score row, advance the **release chain** (`releases/<n>/` holding the four prompt files, `parent`, `component_changed`, `delta.json`, `scores.json`, `probe.json`). The chain is append-only; the head is a symlink/`HEAD` file.
* On reject: record `rejected/<n>/` with the same content; the champion is unchanged; the Reflector is shown the rejection next round (so it does not retry the same mechanism).
* Failed-checkpoint promotion (from Reef): checkpoints the champion fails on the full set are added to the probe set, capped at `PROBE_PROMOTED_MAX`, so the gate tracks the current weak term.

### 3.6 run.py
`python -m loop.run --domain office --iterations 20 --fake-llm` runs end to end offline. Config via env with the same names as the old `config.py` where they exist (`VLLM_BASE_URL`, `EVALUATOR_MODEL`, `JUDGE_MODEL`, `OPENROUTER_*`, `MEMORY_LLM_*`). Artifacts in `runs/<run_id>/iter_<n>/`.

### 3.7 fake_llm.py
A deterministic stand-in for every model call (memory, answerer, judge, reflector, curator), keyed on prompt hash with a small rule-based behaviour so tests exercise real paths: the memory fake classifies by keyword, the judge fake uses the rule scorer only, the reflector fake picks the component with most attributed failures, the curator fake appends one bullet. Set by `SHM_FAKE_LLM=1` or `--fake-llm`.

## 4. What is frozen vs evolvable

| Frozen | Evolvable (outer loop) |
|---|---|
| `keyring.py`, `llm.py` | `prompts/front_door.md` |
| envelope columns, DDL vocabulary, predicate grammar | `prompts/processor.md` |
| op vocabularies in `ops.py` | `prompts/librarian.md` |
| the gate in the retriever, the fallbacks | `prompts/retriever.md` |
| answerer prompt, judge prompt, scoring | |
| reflector/curator prompts (these are the optimiser, not the system) | |

## 5. Layout

```
shm_evolve/
  DESIGN.md  README.md  pyproject.toml  contracts.py
  memory_system/
    agent.py keyring.py(frozen) llm.py(frozen) envelope.py ops.py trace.py
    front_door.py queue.py processor.py librarian.py retriever.py
    prompts/{front_door,processor,librarian,retriever}.md   # v0 hand-written seeds
  loop/
    config.py llm_client.py fake_llm.py data.py evaluate.py answerer.py
    judge.py attribution.py reflector.py curator.py gate.py releases.py run.py
    prompts/{reflector,curator}.md
  tests/
    test_contract.py (ported)  test_invariants.py  test_envelope.py  test_ops.py
    test_pipeline_fake.py  test_evaluate.py  test_judge.py  test_gate.py  test_run_offline.py
    test_vocabulary_lint.py
```
## 6. Implementation notes (as built)

Both builders deviated from this document in places; this section records
where the code and this spec disagree, tersely, citing `file:function`. The
code is authoritative for these points until this doc is rewritten around
them.

* **The answerer decides the final action, not the memory.** `render_answer`
  (`loop/answerer.py`) sets `action = parsed.get("action") or agent_action`,
  i.e. the model's own JSON reply wins when it parses; the memory's own
  choice is kept separately at `output.debug.agent_action` in
  predictions.jsonl (`render_one`, `loop/evaluate.py`).
* **Judge prompt.** `render_judge_prompt` (`loop/judge.py`) renders
  `bench/prompts/judge_prompt.txt`, matching what `bench/scripts/
  score_predictions.py` itself uses -- not `judge_prompt_gatemem.txt` as an
  earlier draft of §3.2 named (fixed above).
* **Probe promotion is by episode, not only by checkpoint.**
  `promote_failures` (`loop/gate.py`) caps NEW episodes it has to run at
  `PROBE_PROMOTED_EPISODES_MAX` (`cfg.probe_promoted_episodes_max`), on top
  of the checkpoint cap `PROBE_PROMOTED_MAX`, because one-pass evaluation
  means promoting one checkpoint runs its whole episode in every probe.
* **Champion full-eval reuse + verdict cache.** `Run.champion_eval`
  (`loop/run.py`) reuses a release's cached full evaluation
  (`self._evals_index()` / `evals.json`) instead of re-running it each
  iteration; `judge(..., cache_path=...)` (`loop/judge.py`) separately caches
  individual verdicts keyed by `_cache_key(checkpoint_id, output, use_llm)`
  in `run_dir/judge_cache.jsonl`, shared across the whole run.
* **Rejections under `rejected/<iteration>/` cover more than gate losses.**
  `Run._reject` (`loop/run.py`, via `ReleaseChain.reject` in
  `loop/releases.py`) is also called when the reflector finds nothing
  (`diag is None`) and when the curator's delta fails lint or is empty
  (`not cur.ok`), not only on a gate REJECT.
* **Evidence lives in `output.debug.evidence`.** `render_one`
  (`loop/evaluate.py`) puts the agent's raw evidence (record_id/text/speaker)
  there, separate from the benchmark's own `output.evidence` shape, so the
  judge and `loop/attribution.py` can see sources the benchmark's own schema
  does not carry.
* **The `_ledger` table (not named in §2.3/§2.4).**
  `Librarian._ledger_blocks` / `_bounded_audience` (`memory_system/
  librarian.py`) refuse a widening (add/split/supersede/grant) for a
  principal a tightening already named, when the widening's chunk is no
  newer than the tightening; rows are written by `_op_revoke`/`_op_restrict`.
* **Policy facts live in a `_policy` side table, not as sealed records.**
  `memory_system/directory.py`'s `add_fact`/`seed_policy`/`current_facts`;
  the table is declared in `memory_system/envelope.py`'s `_SIDE_TABLES`.
* **Supersede inherits audience only conditionally.**
  `Librarian._op_supersede` (`memory_system/librarian.py`) inherits the old
  record's audience only when `chunk.speaker_id == old["speaker_id"] or
  chunk.speaker_id in old["about"]` -- a correction from anyone else inherits
  nothing.
* **`delete` also shreds the keys.** `envelope.tombstone`
  (`memory_system/envelope.py`) calls `keyring.shred` unconditionally; the
  only difference `_op_delete` vs. `_op_shred` (`memory_system/
  librarian.py`, both via `_remove(..., shred=...)`) makes is whether the
  ciphertext body is also nulled. §2.3's table reads as if `delete` left keys
  live; it does not.
* **`about` may hold a declared non-principal entity id.**
  `Librarian._about` (`memory_system/librarian.py`) keeps any value
  `Directory.is_declared` allows, and `is_declared` (`memory_system/
  directory.py`) is true for entities as well as principals.
* **`scope` is an optional arg on `grant` too, not just `revoke`/`restrict`.**
  `Librarian._scope` (`memory_system/librarian.py`) is used by `_op_grant`,
  `_op_revoke` and `_op_restrict` alike; `ops.REQUIRED_ARGS` omits it only
  because it is optional, not because grant lacks it.
* **Retriever events are held until the next ingest/close.**
  `Tracer.emit(..., defer=True)` (`memory_system/trace.py`), called from
  `Retriever.answer` (`memory_system/retriever.py`), is flushed by
  `Tracer.commit_pending` at the start of `GateMemAgent.ingest`/`reset` and by
  `GateMemAgent.close` (`memory_system/agent.py`) -- a caller that never
  calls `close()` at episode end loses the last query's events from the
  table (though not from the JSONL mirror); `loop/evaluate.py:run_episode`
  does call it.
* **Trace op labels outside the closed `ops.py` vocabulary.**
  `front_door.py` emits `chunk:<kind>` (e.g. `chunk:delete`); `librarian.py`
  emits `signal` for integrity signals; `retriever.py` emits `query`;
  `loop/attribution.py` further names the retriever's two internal stages
  `search`/`select` in its OWN attribution (not in the trace itself).
* **Side tables are prefixed `_`** (`_policy`, `_ledger`, `_restrictions`,
  `_expiries`, `_links`, `_meta`, `_collections`, `_fields`, `_views`,
  `_record_index`, `_terms`): `memory_system/envelope.py`'s `_SIDE_TABLES`;
  `ops.is_identifier` requires a leading letter, so no structure op can ever
  name or shadow one.
* **Record ids are `r00001`, `r00002`, ... per store**, not globally unique
  across stores: `envelope.new_record_id` (`memory_system/envelope.py`), a
  per-connection `_meta.next_record` counter.
* **`QUEUE_BATCH` defaults to 1, and the invariants are tested at that
  default.** With batch 1, `ChunkQueue.ready()` (`memory_system/queue.py`) is
  true after every `push`, so `GateMemAgent.ingest` (`memory_system/
  agent.py`) always drains the queue before returning, and `query`'s own
  `if len(self.queue): self.queue.flush(...)` finds nothing to do. At
  `QUEUE_BATCH>1` that flush would run the processor/librarian -- including
  `keyring.grant` -- INSIDE `query`, which `tests/test_invariants.py::
  test_grant_is_never_called_during_query` and DESIGN.md §2.8 invariants 2
  and 7 only hold at the default; `tests/test_invariants.py::
  test_query_after_its_own_flush_is_byte_identical` (`queue_batch=50`)
  exercises that other side directly.


### §6.1 Changes from the first live runs (2026-10-14)

- **Model filters rank, never exclude** (`memory_system/retriever.py::candidates`): a `where`/`about` that
  matches satisfies ranks first; the set is topped up from the word-matched pool without them. Found when
  the operator-contract fix made the model's bad filters effective and emptied a third of the searches.
- **A bad `where` clause drops the clause, not the spec** (`retriever.py::_search`).
- **Thinking switch** (`llm.py::complete(thinking=)`, `prompting.thinking_for`): `MEMORY_LLM_THINKING_COMPONENTS`
  names `<component>` or `<component>:<stage>`; an empty reasoning reply is retried once without reasoning.
- **Held-out split** (`loop/data.py::select_split`, `run.py::_ensure_heldout`): `HELDOUT_EPISODES_PER_DOMAIN`
  episodes per domain, pinned in `run.json`, scored per head release into `releases/<n>/heldout_scores.json`,
  never attributed, probed or promoted.
- **Gate**: pairs every checkpoint of every probe episode; requires paired MGS not lower; with several
  training domains, no domain's paired MGS may fall by more than `GATE_DOMAIN_FLOOR` (0.02).
- **Reuse key** (`evaluate.py::prompts_hash`) now covers the memory_system source and the thinking config.
- **Timeouts**: `OPTIMIZER_TIMEOUT_S` (900) for reflector/curator; `EPISODE_TIMEOUT_S` default 7200, measured
  as running time (semaphore-bounded submission).
- **Operations**: `tools/llm_tap.py` logs every call and retries 429/5xx; `SSL_CERT_FILE` bundle for the
  endpoint's incomplete chain; kill pool children before resuming (SQLite journals are opened by path).
