# GateMem evaluation adapter

The GateMem benchmark is present in this repository (`../bench/data/<domain>/{episodes,checkpoints}.jsonl`,
scorer in `../bench/eval/scorer.py`). GSM plugs in as an **external method**: it writes `predictions.jsonl` in
the documented format and hands scoring to the official scorer. GSM does not re-implement GateMem's formulas.

## Integration boundary

```
bench/data/<domain> ──► gsm.eval.gatemem.sanitize.load_for_method   (method side; allowlist)
                          │   MethodEpisode(principals, relationships, turns)
                          │   MethodCheckpoint(checkpoint_id, episode_id, as_of_turn_id, asker_principal_id, query_text)
                          ▼
                        GateMemRunner ──► GovernedMemoryService (one tenant per episode)
                          ▼
                        predictions.jsonl, method_metrics.jsonl, run_summary.json
                          ▼
bench/data/<domain> ──► gsm.eval.gatemem.scoring.score_run          (scoring side; only reader of hidden labels)
                          └─► bench.eval.scorer.score_predictions   (official U, A, F, OR, MGS)
```

* **Hidden fields are stripped by allowlist.** `query_type`, `expected_action`, `judge_spec`, `attack_type`,
  `leak_targets` and any unknown field never pass `sanitize_checkpoint`. `MethodCheckpoint` is
  `extra="forbid"`. The checkpoint's `asker.role` is not forwarded either: GSM resolves roles from its own
  bindings.
* **The isolation is tested.** `tests/gatemem/test_gatemem_adapter.py::test_hidden_fields_never_reach_any_component`
  plants unique `CANARY-*` strings in every hidden field of hand-written fixtures. It then wraps every
  component (ingestion commit, policy read/write, planner, ranking, generator, release verifier, semantic
  verifier), runs the full adapter, and asserts that no canary appears in any recorded input or anywhere in
  the database.
* **Fixtures are synthetic.** They live in `tests/gatemem/fixtures/` and are not copied from the official data.

## Episode mapping

* **Principals and roles.** Episode principals become GSM principals, and their roles become role bindings.
  GateMem's authenticated askers are issued contexts by a trusted harness authenticator
  (`issued_by="gatemem-harness"`).
* **Relationships.** Heterogeneous relationship records are mapped generically:
  * the subject is the first of `principal_id` / `clinician_id` / `family_id`;
  * the object is the first of `project_id`, `patient_id`, `for_principal_id`, `manages`, `program_id`,
    `case_id`, `household_id`, `for_household_id` or `guest_id`;
  * the relation is the normalized `type`.
  Remaining attributes (`access_scope`, `access_level`, `scope`, ...) are shown to the model interpreter
  alongside the group they qualify. GSM does not enforce them itself.
* **Episodes run in parallel** (`--workers N`), each with its own clock, service instance and tenant. Turns
  within an episode are always processed in order.

## Turn interpretation

There are two interpreters (`--interpreter`).

### `heuristic` (default)

`DeterministicTurnPolicy` is a transparent baseline, not a claim of good GateMem performance:

* each turn becomes a note owned by its speaker;
* turns with privacy cues are speaker-private (confidential);
* other turns are readable by principals who share a relationship object with the speaker;
* turns with deletion cues are lifecycle commands and are not stored.

Deletion resolution (`--resolve-deletions`):

* `none` (default): natural-language deletion requests are not acted on.
* `lexical`: the harness, acting for the authenticated speaker, deletes what a governed query surfaces for
  the request. GSM authorizes each deletion.

### `qwen`: model-backed (`gsm/eval/gatemem/qwen_interpreter.py`)

The model **labels** each turn, and trusted code (`LabelMapper` in `adapter.py`) **decides** what that means
for GSM.

**What the model sees.** Only the ingestion-time material:

* the episode roster (principals with roles, numbered relationship groups with their scope attributes, the
  role list);
* up to 12 recent standing instructions (turns it labeled as consent / sharing / retention rules);
* the 3 previous turns, for references like "yes, that's fine". Deletion requests are replaced by a
  placeholder;
* the semantic keys of memories the speaker can fully see;
* the message itself.

It never sees checkpoint queries or scoring annotations. A test records every request body and checks this.

**What the model returns** (strict JSON, validated with pydantic; prompt version `gatemem-turn/v7`):

* the turn kind: fact, instruction, deletion or chatter;
* a neutral topic;
* who the information is about, and who the message explicitly says must not be told (`exclude`);
* the full audience, and an audience for less detailed "shareable" versions;
* the exact protected values in the turn;
* the information as **exact quotes** of the message (at most 6), one per independent piece of information
  (what the message presents as one current state, such as a list of appointments, stays one quote), each
  with an optional semantic key (only for the current value of something that can change) and an optional
  shareable version;
* for deletions and corrections, what earlier information to remove.

Two small **separate** calls (`gatemem-aux/v1`) add the adaptive behaviour without touching the labelling
prompt, so every labelling decision stays exactly as in v4 (a v5 attempt that folded them into the labelling
prompt shifted unrelated audience choices, e.g. 20% → 31% of office turns labelled "everyone"):

* for fact and instruction turns, whether the message **only asks a question**;
* for instruction turns, whether it is a **consent or revocation** about earlier information, which
  information, and who should now be told (`share_with`) or no longer be told (`withhold_from`).

The prompt's examples are invented and appear nowhere in the benchmark data.

**What trusted code does with it:**

* **Closed menu.** Audiences come only from this episode's groups, roles, named principals, "speaker",
  "about" and "everyone". Unknown tokens are dropped and counted.
* **Owners see their information; mentioned people do not.** The model reports `owners`: the people whose own
  information the message is (a patient's health details, an employee's personal details). Owners are always
  in the full audience of the quotes that hold their own information, and hold grant authority there (they
  may consent to sharing further); a separate call (`gatemem-aux/v1`) assigns owners per quote, so a
  contractor who owns "Leo remains logs-only" does not also own the incident diagnosis in the same message. People who are only
  mentioned get nothing automatically. (Two earlier versions were measured: adding everyone the message is
  about let a contractor mentioned in a security recap read the incident diagnosis; adding nobody made
  patients lose access to their own care information, medical U 0.695 → 0.486.) Someone the message names
  and explicitly says not to tell gets an explicit memory-level denial, which wins over ownership.
* **Floor.** If the turn has privacy cues or protected values, "everyone" is removed from the full audience.
  A shareable version that repeats a protected or retired value is discarded. Protected values must appear
  verbatim in the turn.
* **One memory per quote.** Each quote is stored as its own `tiered_note`: the full view is the quote, the
  redacted view the shareable version. All quotes of a turn share its audience. Protected values are stored
  with each, so the release verifier fingerprints them.
* **Quotes are verified.** A quote must occur in the message (up to case, punctuation and spacing). The
  speaker's own wording is kept: no paraphrase, no added names or ids. (A first version, v3, stored
  model-written restatements; they dropped the wording later questions reuse and named patients in every
  statement, which made the baselines' answerer refuse staff more often.)
* **Completeness guard.** If a quote is not in the message, or a protected value or number of the message is
  in no quote, the message itself is also stored (unkeyed), so splitting never loses a value
  (`quotes_not_verbatim`, `residual_messages_kept`).
* **Authority.** Anyone in a memory's full audience may update or delete it; GateMem deletion requests
  usually come from someone other than the author. Existence is acknowledged to episode participants, so
  over-scoped requests get REFUSE. Deleted memories are never acknowledged.
* **Supersession per quote.** A quote whose key matches an earlier memory's key supersedes only that memory,
  when GSM authorizes the change, and only if the two are about the same thing: keys are model-chosen and
  sometimes too coarse, so the old and new text must also be at least `GSM_VECTOR_MIN_SIMILARITY` similar (the
  calibrated level above which a match needs no lexical support); otherwise both stay current
  (`supersede_skipped_dissimilar`). Two quotes of one message never supersede each other. When the
  supersession is refused as a broadening (a non-owner sharing the new value with a different audience), the
  speaker retires the outdated value (`RETIRE_FACT`, narrowing only, authorized by GSM) and the new statement
  is stored under its own policy (`retired`).
* **Deletions and corrections.** When the message asks to remove something, the harness first runs governed
  queries *as the requester*; the model picks targets only among those results, and each target is deleted
  as the requester and authorized by GSM. Then the message's current statements are stored, **whether or not
  anything was deleted**. Values recorded on the deleted memories (their protected values and numbers) are
  masked out of the new statements (`[removed]`) when the message does not restate them or explicitly names
  them as what to remove. The request text itself is never stored.
* **Oversight groups get protected values withheld (opt-in, `--oversight-withhold`).** Once per episode a separate call classifies each
  relationship group as doing the work, oversight only (responsible for no part of the work, only kept
  informed, e.g. executive sponsors), or scope-limited. When a message's audience includes an oversight group,
  its quotes carrying protected values reach that group with the values replaced by `[withheld]`. Measured
  (v14 vs v13): it fixed the executive-budget leaks but cost more office utility than it saved (11 utility
  checkpoints lost vs 3 leaks fixed), so it is off by default.
* **Scope-limited groups get the in-scope part (opt-in, `--scope-fit`).** When a stored message leaves out a scope-limited group
  (e.g. a family member with scheduling-only access), a separate call writes, per quote, a version keeping only
  the part inside that group's stated scope ("Repeat test: Thursday, March 5 at 8:10 AM, lab suite"); it
  becomes that group's redacted view, and is rejected if it carries a protected or retired value. Measured
  (v15 vs v13): no gain beyond run-to-run noise at ~5,000 extra calls per run, so it is off by default.
* **Revocation overrides ownership (opt-in, `--revocation-override`).** Re-scope extraction also runs on deletion-labelled turns ("his access
  has been revoked"). Anyone a message cuts off is excluded from that message, and from then on is no longer
  given messages automatically as their owner or subject (the model's audience may still name them).
  Measured (v14 vs v13): it also hid from family members the patient's own statements limiting them, and
  without those the answerer refused less, so leaks rose; off by default.
* **Need-to-know for exact values (opt-in, `--need-to-know`).** When a turn has protected values (amounts, customer names, tokens,
  diagnoses, ...) and its audience includes groups or roles, a separate call (`gatemem-aux/v1`) names the
  audience tokens that only oversee, sponsor, assist, coordinate, advise or partner, or have a limited scope.
  Those keep access, but quotes carrying protected values reach them only as a deterministic redacted view
  with the values replaced by `[withheld]`; quotes without protected values they see in full. The speaker and
  the people the information is about are never demoted (`need_to_know_demoted`). Measured on GateMem
  (v9 vs v7) it removed some office leaks (A 0.228 → 0.170) but the model demoted far too broadly, and
  utility fell in both domains (MGS office 0.571 → 0.431, medical 0.637 → 0.530), so it is off by default.
* **Questions are not facts.** A message that only asks something is not stored (it stays visible to the
  interpreter as recent context). As a guard, a quote that is itself a question is dropped, and a turn whose
  only quotes are questions stores nothing (`questions_detected`, `questions_dropped`).
* **Failures.** Invalid or failed model output falls back to the heuristic for that turn
  (`interpreter_fallbacks`).
* **Caching.** Responses are cached by a hash of the full prompt (`--interp-cache`), so a re-run with the
  same inputs makes no model calls.

Thinking is off by default for the interpreter (`--interp-thinking` turns it on). On Qwen3.8-27B that is
~0.3 s per call with thinking off versus ~2.5 s with it on, measured on a short prompt.

### Adaptive re-scoping: consent and revocation change earlier memories

Standing instructions always shaped how *later* turns are labelled. With re-scoping, a consent or revocation
also changes who may know information stored *earlier*, so the memory's access structure evolves within an
episode as the conversation does:

1. For each instruction turn, a separate call reports `rescope: {what, share_with, withhold_from}` when the
   message changes who may know earlier information ("stop sharing my results with my brother", "you can tell
   my sister the appointment time"). Audience tokens come from the same closed menu (a bare roster id counts
   as `principal:<id>`) and are resolved to principals.
2. As with deletions, governed queries run *as the speaker*, and the model picks the memories the instruction
   applies to only among what the speaker can see.
3. Each change is a GSM operation submitted as the speaker, and GSM decides:
   * **withhold** → `RESTRICT_ACCESS` (explicit memory-level denials). It narrows, so anyone who may update the
     memory (its full audience) may do it;
   * **share** → lift any earlier restriction, then `GRANT_ACCESS` (FULL view). It widens, so only the memory's
     owner or the people it is about may do it: every mapped memory gives its subjects grant authority, so a
     patient's consent can share their own records, but a colleague cannot widen someone else's.
4. Denials outrank grants and rules, so a revocation holds even against the groups chosen at storage time.

Counters: `rescope_requests`, `restrictions_committed` / `_refused`, `grants_committed` / `_refused`,
`rescope_no_candidates`; the ingest log records the targets and outcomes per turn.

### Utility repairs (`gsm/eval/gatemem/repairs.py`)

Fixes for the GSM-side reasons in the v13 utility-failure analysis (`analysis/utility_reasons.py`). All are on
by default; `--disable-repair NAME` (repeatable) switches one off, and `run_summary.json` records the set.

| repair | reason | what it does | where |
|---|---|---|---|
| `delegate_floor` | R3 audience too narrow | a scope-limited group (one with relationship attributes such as `access_scope`) in an audience brings in the unrestricted groups on the same object, e.g. a device-setup delegate → the household members; oversight-classified groups are not added. Runs the per-episode group classification | `delegate_floor`, `LabelMapper._floored` |
| `keep_readers` | R3 | an update (supersede, or retire-and-add) keeps the old record's full-view readers, its speaker and grants, and carries over its explicit denials; narrowing is left to instructions | `_EpisodeSession._keep_readers` |
| `scope_records` | R4 scope view missing | with `--scope-fit`, groups whose in-scope versions differ each get their own record (keyed `<key>.scope_<groups>`), instead of all sharing the first version | `LabelMapper._scope_record` |
| `carry_forward` | R5 partial restatement | on a supersede, the model names the parts of the old statement still in effect (`CARRY_SYSTEM`); parts must be verbatim in the old record, not restated, not holding a retired value, not naming the same slot as the new value ("room 4B" vs "room 7C"), and not holding an old protected value for readers the old record did not have. Kept parts are appended: `... (still in effect from before: ...)` | `_EpisodeSession._carry_forward`, `_same_slot` |
| `partial_delete` | R6 collateral deletion | deletion selection names, per item, the exact part to remove (`SELECT_PARTS_SYSTEM`); an item that also holds current facts is deleted and stored again without that part (same key, policy, denials, grants, source turn); retired values come from the removed part only | `_EpisodeSession._rewrite` |
| `delete_history` | R6 / U1 earlier versions outliving a deletion | a deletion request also offers the model the earlier versions (superseded, or retired by an update) of the speaker's readable records that share words with the request; picked ones are deleted whole, and deleting a record deletes its earlier versions | `_EpisodeSession._history`, `_model_delete` |
| `activity_notes` | U1 answerer lacks the asker's role context | a message labelled chatter that states the speaker's own role or task ("I'm reconciling the home medications") is kept, readable only by the speaker, every value masked (`ACTIVITY_SYSTEM`) | `_EpisodeSession._store_activity` |
| `own_activity` | U1 | with the baselines' answerer, the asker's own superseded and retired records are appended after the evidence, values masked (`_mask_values`) and marked `[earlier, since updated]`; only while the information is still held (the chain of replacements ends in a current record), never records that held sensitive values, were deleted, or the asker was restricted from | `_EpisodeSession._own_activity`, `_MeteredGenerator` |

Always on (bug fixes, R6): dates, clock times and codes are whole values in `_values` ("July 2, 2026" no longer
retires "2026"), and `_mask` replaces whole tokens only. R4 also needs `--scope-fit`, which stays opt-in (it
measured neutral to negative on medical and office; use it for education and household).

Note: the label prompt lists the memory keys the speaker can see, so any change in audiences changes later label
prompts; a run with different repairs re-labels most turns instead of hitting the cache.

### Retrieval options for runs

* **Query decomposition (`--planner decompose`, `--planner-cache`).** The model (thinking off) splits a
  multi-part question into up to 5 atomic sub-questions. It sees only the question. Retrieval ranks the whole
  question and each part against its own relevance bar and merges them round-robin up to `--top-k`. The
  answerer still receives the original question. See [RETRIEVAL.md](RETRIEVAL.md#multi-part-questions).
* **Sentence embeddings (`GSM_EMBEDDINGS_PROVIDER=sentence_transformers`).** `all-MiniLM-L6-v2` on CPU, the
  model the baselines embed with (`pip install 'gsm[embeddings]'`; set `HF_HOME`, and `HF_HUB_OFFLINE=1` to use
  a cached copy).
* **Thresholds, calibrated without labels.** `gsm gatemem calibrate --data-dir ...` reads dialogue turns only
  and measures similarity between sentences of different episodes, which are unrelated by construction:

  | domain | `GSM_VECTOR_MIN_SIMILARITY` (p99 of noise) | `GSM_VECTOR_SUPPORT_SIMILARITY` (p25 of lexical-overlap noise) |
  |---|---|---|
  | office | 0.597 | 0.147 |
  | medical | 0.456 | 0.132 |

### Answerer: the baselines' own (`--answerer baseline`)

For comparisons against the GateMem baselines, the answer step is the benchmark's own answerer, not GSM's.
`gsm/eval/gatemem/baseline_answerer.py` subclasses `bench.agents.base.BaseMemoryAgent` and calls its
unmodified `_run_llm`:

* the same `bench/prompts/query_prompt.txt`, variables, JSON parsing and fallbacks;
* the same `bench.llm.router.LLMRouter`, with the `qwen38_27b` settings of `configs/sweeps/paper_matrix.yaml`:
  `llama` provider, `Qwen/Qwen3.8-27B`, temperature 0.2, 16384 max output tokens, 1800 s timeout, 3 retries,
  and thinking left at the server default.

Only the retrieved memory differs: it is GSM's permitted evidence, rendered by the bench as
`Memory i (speaker=...)` (speakers are shown for full views only). The answerer never receives hidden
checkpoint fields; the bench uses `query_type` / `expected_action` only in stub mode, and they are passed
empty. The asker's authenticated role, which the baselines' prompt uses, is forwarded.

**How GSM bounds the answerer** (`release_action_policy = "answerer"`):

* **Declining is always allowed.** The answerer saw only permitted evidence, so choosing refuse or no_memory
  cannot leak anything. GSM releases its canonical text for that action (reason `ANSWERER_DECLINED`), never
  the model's free text.
* **Content needs evidence.** An answer / answer_redacted is accepted only when GSM retrieved permitted
  evidence. It then passes every content check: approved citations only, keyed fingerprints of hidden
  values, and prohibited categories. The answer / answer_redacted label is the answerer's.
* **Failures are safe.** A failed check falls back to GSM's deterministic rendering of the permitted evidence;
  an answer with no evidence becomes NO_MEMORY.
* **Only verified text is released.** The bench's `answer_structured` is not released (it is not verified), and
  the query cache is off so every checkpoint reaches the answerer.

### Running with a local Qwen3.8-27B (vLLM)

```bash
export GSM_DATABASE_URL=$(scripts/local_pg.sh url)
export GSM_LLM_BASE_URL=http://localhost:8003/v1 GSM_LLM_MODEL=Qwen/Qwen3.8-27B
export GSM_LLM_API_KEY=<the server's --api-key> GSM_LLM_TIMEOUT_S=180
export LOCAL_API_KEY=<the server's --api-key>   # read by the baselines' answerer, as in the sweep config

export GSM_EMBEDDINGS_PROVIDER=sentence_transformers HF_HOME=<cache dir> HF_HUB_OFFLINE=1
export GSM_VECTOR_MIN_SIMILARITY=0.456 GSM_VECTOR_SUPPORT_SIMILARITY=0.132   # medical, from `gsm gatemem calibrate`

gsm gatemem run --data-dir ../bench/data/medical --out outputs/gatemem/qwen_medical \
    --interpreter qwen --interp-cache outputs/gatemem/cache/qwen38_interp.jsonl \
    --planner decompose --planner-cache outputs/gatemem/cache/qwen38_decompose.jsonl \
    --answerer baseline --top-k 20 --workers 21
gsm gatemem score --data-dir ../bench/data/medical --run-dir outputs/gatemem/qwen_medical   # rule-based
```

To judge with Qwen3.8-27B, use the scorer that judged this repository's Qwen baselines
(`outputs_qwen38-27b_judge/`), so GSM and the baselines are scored by the same instrument:

```bash
cd "../Auto Research"
# MOCK_MODE=0 and JUDGE_TRANSPORT=http are required: the defaults are a mock client and the dsh transport,
# which silently fall back to the rule verdict for every checkpoint.
MOCK_MODE=0 JUDGE_TRANSPORT=http JUDGE_MAX_TOKENS=2048 \
GATEMEM_DOMAIN=medical JUDGE_ROUTE=openai JUDGE_MODEL=Qwen/Qwen3.8-27B \
OPENAI_BASE_URL=http://localhost:8003/v1 OPENAI_API_KEY=<the server's --api-key> \
  ~/miniconda3/envs/gatemem-auto/bin/python score_baseline.py ../gsm-rebuild/outputs/gatemem/qwen_medical \
  --out ../gsm-rebuild/outputs/gatemem/qwen_medical_judge.json
```

That scorer gates utility and leakage on the action label, and it counts a missing checkpoint as both a
privacy and a deletion leak. Score full-domain runs, or pass `--limit N` for partial ones.

## Outputs and metrics

`predictions.jsonl` uses the `run_eval.py` schema. `output.memory_audit.prompt_context.text` contains exactly
the projections the generator was shown, so GateMem's context-exposure metrics measure GSM honestly.

`gsm gatemem score` writes `gsm_eval_report.json` with the following fields.

| Field | Source |
|---|---|
| `official.utility_accuracy` (U), `privacy_leakage_rate` (A), `deletion_leakage_rate` (F), `over_refusal_rate`, `compliance_utility_score` (MGS = U·(1−A)·(1−F)), `action_accuracy`, context/e2e variants | official scorer, unmodified |
| `gsm.verifier_false_positives/negatives` | method metrics joined with labels *after* the run (definitions in the report) |
| `gsm.deletion_propagation` | GSM cascade jobs: completed/total, descendants found/tombstoned |
| `gsm.latency_ms`, `gsm.tokens`, `gsm.estimated_cost_usd` | per-checkpoint method metrics; prices from `GSM_LLM_PRICE_*` |
| `gsm.authorized_retrieval_recall` | `null`: GateMem has no per-record authorization labels. Measured instead on GSM's own fixtures |

The report uses GateMem's legacy field names. `paper_metric_names` maps them to paper terms. If the official
scorer cannot be imported, `official` is `null` and nothing is approximated.

## Reference run (sanity check, not a result)

One office episode (238 turns, 32 checkpoints), deterministic providers, `--resolve-deletions lexical`,
official **rule-based auxiliary** scorer (no LLM judge):

| U | A | F | OR | MGS | action acc. | p50 latency |
|---|---|---|---|---|---|---|
| 0.556 | 0.30 | 0.615 | 0.0 | 0.15 | 0.28 | 25 ms |

These numbers show that the pipeline runs end to end on real data and hands off to the official scorer. They
also show that the weak link is interpretation: the turn heuristic cannot read natural-language sharing and
deletion instructions. The deterministic generator never produced text outside the permitted evidence. Every
leak counted here comes from that evidence having been authorized by a naive policy, not from a governance
bypass. Meaningful GateMem numbers need a model-backed interpreter (see [SECURITY.md](SECURITY.md#future-work-highest-value-first)).

```bash
gsm gatemem run   --data-dir ../bench/data/office --out outputs/gatemem/office --max-episodes 1 --resolve-deletions lexical
gsm gatemem score --data-dir ../bench/data/office --run-dir outputs/gatemem/office
```
