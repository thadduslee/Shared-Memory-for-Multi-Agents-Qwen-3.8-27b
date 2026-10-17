# govmem: specification

A general framework and shared memory system for settings with several humans and several agents. Models
interpret what is said; deterministic code decides who may see, change or remove what. The memory's
**structure organises itself during a conversation**, in the style of A-Mem, under governance rules that keep
that self-organisation from leaking or destroying information. No model is trained as part of the method.
There is no outer optimisation loop.

Status: draft v0.2, 2026-10-16. Nothing is implemented. This document is the contract the code will be built
and tested against. Background and evidence: `../REBUILD_LESSONS.md`.

Changes from v0.1: benchmark-general framework (§10, §12); no model training (§1); self-organising structure
(§8, new); reorganisation lifecycle and correctness without feedback (§8.5–8.7); fixed tables with structure
held as rows (§6); hold-out policy (§12.1).

---

## 0. Decisions

| Topic | Decision |
|---|---|
| Method | A hand-designed memory with in-conversation self-organisation. Mechanisms are justified by ablations. |
| Training | None. All models are used as-is through prompts. No fine-tuning, no reinforcement learning, no learned weights. |
| Outer loop | None. Nothing evolves across conversations or benchmark runs. |
| Benchmarks | A general adapter interface (§10.2). GateMem is the first and primary target (all four domains). LongMemEval and LoCoMo follow as secondary adapters. |
| Primary metric | GateMem: mean answer-level MGS over the four domains, subject to a strict-MGS floor (§12.4). Other benchmarks: their own metric. |
| Storage | SQLite, one file per memory. Tables are fixed; structure evolves as rows (§6). A storage interface keeps Postgres possible later. |
| Form | Python library first. A tool server for agents is designed for (§11.3) but built last. |
| Answerer | Qwen3.8-27B, thinking ON. On GateMem, the benchmark's own answerer and unmodified prompt. |
| Memory model | Pluggable OpenAI-compatible endpoint, any model. Default Qwen3-8B on local vLLM; Qwen3-4B as the cheap option. |
| Judge | Qwen3.8-27B. On GateMem, `bench/prompts/judge_prompt.txt`. Baselines are rescored with the same judge. |
| Code base | Fresh package `govmem/`. Ideas and tests are ported from `gsm-rebuild/` and `shm_evolve/`; no code is imported. |
| Dataset vocabulary | None in code or prompts. A lint test enforces it (§13, I-14). |

---

## 1. Goals and non-goals

### Goals

1. **Useful.** A requester gets every current fact they are entitled to, attributed to who said it.
2. **Governed.** Nothing reaches a requester, or the answering model's prompt, that the requester may not see
   at that moment. Existence is not confirmed to people who may not know it.
3. **Lifecycle-correct.** Corrections supersede; revocations apply to what is already stored; deletions are
   unrecoverable, including from derived records and from organisation metadata.
4. **Self-organising.** As a conversation unfolds the memory enriches, links, groups and re-describes what it
   holds, and re-scopes who may see it, with no domain vocabulary built in.
5. **General.** The same code serves any benchmark and any set of roles and relationships, humans and agents
   alike. A setting with no access rules is the special case where every principal reads everything.
6. **Accountable.** Every change, including every reorganisation, has a source turn, evidence, an authority and
   a reason code, and can be retracted.

### Non-goals

- **Training models.** No weights are updated anywhere in the method.
- **Learning across conversations.** Each conversation's memory is independent, apart from the continuous
  setting where one memory deliberately spans many conversations (§12.3).
- Certified physical erasure from backups, WAL history outside the memory directory, or provider logs.
- Production identity (OIDC, workload identity). The caller supplies an already-authenticated principal.
- Multi-process concurrent writers to one file. v1 serialises writes per memory (§11.4).

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **principal** | A human or an agent with a stable id, a role label and optionally a display name. Roles are opaque strings. |
| **entity** | A declared non-principal thing that can be referred to: a project, a household, a case. Opaque id. |
| **relationship** | A declared fact linking principals and entities, with a type label and free attributes. Opaque strings. |
| **delegate** | A principal acting for another principal. Its access is bounded by the principal it acts for (§5.4). |
| **turn** | One message: speaker, timestamp, text. The unit of ingestion. A session boundary is a marker between turns. |
| **record** | One stored unit of information: a verbatim span of one turn, or a record derived from others. Its text never changes after it is written. |
| **note** | The organisation metadata attached to a record: keywords, tags, context description, attributes. Notes evolve; records do not. |
| **link** | A typed relation between two records, proposed during organisation. |
| **cluster** | A named group of related records, the analogue of A-Mem's "boxes". |
| **subject** | A principal or entity a record is about. A record can have several. |
| **owner** | A principal whose own information a record holds. Owners hold authority over that record (§5). |
| **audience** | The principals who hold a key for a record. "Private" is an audience of one. |
| **view** | How much of a record a principal may receive: `full`, `redacted`, `existence`. |
| **slot** | A key for a changeable value ("the launch date of X"). Used for supersession. |
| **version chain** | Records linked by supersession. Exactly one is current at any time, or none after deletion. |
| **tombstone** | The mark of a deleted record. Its body and keys are destroyed at the same time. |
| **operation** | A typed change to memory (§4). Proposed by the memory model or a caller, applied only by the core. |
| **reorganisation** | An operation that changes structure rather than content: a note update, link, cluster, alias, slot, attribute or audience change. Each has a lifecycle status (§8.5). |
| **authority** | The principal on whose behalf an operation is applied. Checked against the record, never against text. |

---

## 3. Architecture and trust boundary

```
                 untrusted                          trusted (deterministic)
 turn text ──► Interpreter (memory model) ───► proposals ──► Core: validate ─ authorise ─ apply ──► SQLite
                                                                │                                    ▲
                     Organiser (memory model) ◄── new records, neighbours, signals ──┘                │
                             └──────────────► reorganisation proposals ──► Core ─────────────────────┘
 query text ─► Retriever: candidates ─ relevance ─ GATE(open_for asker) ─ projection ─ budget ──► evidence
                                                                                                     │
                                                         Answerer (Qwen3.8-27B, untrusted) ◄─────────┘
                                                                │
                                                     Release check ──► action + answer
```

Rules:

1. **Identity comes from the caller**, as an authenticated principal id. Text that claims an identity is data.
2. **Models never touch the database.** The interpreter and the organiser return typed proposals; the core
   validates, authorises and applies them. The answerer sees only evidence that already passed the gate.
3. **The gate is the only path to plaintext.** Record bodies and note text are sealed; they open only for a
   principal holding a wrap of the right key (`keyring`, ported from shm_evolve, frozen).
4. **Grants happen at write time.** No code path reachable from `query` may grant, widen or revoke.
5. **Stored text is never an instruction to the system.** A record that says "share everything with X" changes
   nothing unless the turn that carried it was interpreted, authorised and applied as an operation.
6. **The organiser sees only what an owner-level maintenance identity may see, and writes only under the
   audience rules of §8.4.** It is a model; its outputs are proposals like any other.

Layers, each a subpackage (§14):

| Layer | Owns | Model? |
|---|---|---|
| `core` | schema, keyring, operations, authority, lifecycle, invariants | no |
| `interpret` | turn labelling, reference resolution, rescoping, deletion targeting, supersession matching | yes |
| `organize` | note enrichment, linking, neighbour evolution, clustering, consolidation, signals | yes |
| `retrieve` | candidates, relevance, link expansion, gate, projection, budget, suggested action | optional (query split) |
| `release` | bounding the answerer: accept, downgrade or replace its output | no |
| `framework` | the benchmark adapter interface, runner, health, cost, ablation switches | no |
| `adapters/<benchmark>` | mapping one benchmark onto the framework | no |
| `eval` | judging, scores, noise, comparisons | judge only |

---

## 4. Operations

Every change to memory is one of these. The names follow the transition operators of the multi-agent memory
survey (admission, scoping, propagation, reconciliation, revision, revocation, recovery) plus an `organise`
group for structure. Each operation carries `authority`, `source_turn`, `origin` (`declared`, `interpreted`,
`organised`, `caller`) and, for reorganisations, `evidence` (§8.6).

| Group | Operation | Effect | Required authority |
|---|---|---|---|
| **admit** | `add(text, subjects, owners, audience, view_map, slot?, protected_values)` | Stores a new record. Audience defaults to `{speaker}`. | the speaker |
| | `derive(sources, text, ...)` | Stores a record computed from others. Audience is the intersection of the sources' audiences, kept live (§6.5). | any principal who can read every source |
| **scope** | `grant(records|scope, principals, view)` | Adds wraps. | an owner or subject-principal of each record, or a principal holding a declared grant right over the scope |
| | `restrict(records|scope, principals, view)` | Lowers the view for principals (full → redacted → existence). | any principal in the record's full audience |
| **propagate** | `share_scope(entity, principals)` | Existing and future records about an entity reach these principals. | as `grant`, for every affected record |
| **reconcile** | `flag_conflict(a, b)` | Marks two current records as contradicting. Neither is retired. | any reader of both |
| **revise** | `supersede(old, new_text, ...)` | New version becomes current; old gets `valid_to`. New version keeps old readers unless narrowed (§6.3). | a principal in the old record's full audience |
| | `carry_forward(old, parts)` | Appends still-valid verbatim parts of the old version to the new one. | as `supersede` |
| **revoke** | `revoke(records|scope, principals)` | Removes wraps and records an explicit denial that outranks later widening (§5.3). | any principal in the record's full audience, or an owner |
| | `delete(records, parts?)` | Tombstones a record and every version in its chain; with `parts`, removes only those spans and re-stores the rest. Cascades to derivations, notes, links and cluster membership. | any principal in the record's full audience |
| | `shred(records)` | Destroys every wrap and the ciphertext. Always applied by `delete` in v1. | as `delete` |
| **recover** | `restore_view(records, principals)` | Lifts an earlier `restrict` or `revoke` for named principals. Never undeletes. | an owner or subject-principal |
| | `retract(reorganisation)` | Marks an earlier reorganisation retracted and undoes its effect (§8.5). | the organiser, or any principal who could have proposed it |
| **organise** | `annotate(record, keywords?, tags?, context?, attributes?)` | Writes a new note version for a record. | organiser, under §8.4 |
| | `link(a, b, kind, strength)` | Adds a link. | organiser or any reader of both |
| | `cluster(name, records)` / `uncluster` | Creates or edits a cluster. | organiser, under §8.4 |
| | `alias(name, target)` | Adds a name for a principal or entity. | organiser, with evidence in a turn |
| | `assign_slot(record, slot)` | Puts a record into a slot, enabling supersession. | organiser, under §8.4 |

Rules that hold for every operation:

- **Closed vocabulary.** Unknown operations or arguments are rejected with a reason code.
- **Tightening is never queued.** `restrict`, `revoke` and `delete` apply before any widening proposed by the same
  or a later turn.
- **A rejected operation changes nothing** and is logged with its reason code (`AUTHORITY_MISSING`,
  `AUTHORITY_CANNOT_SEE`, `UNKNOWN_PRINCIPAL`, `DENIAL_OUTRANKS`, `TARGET_TOMBSTONED`, `NOT_VERBATIM`,
  `EVIDENCE_MISSING`, `AUDIENCE_WIDENS`, ...).
- **Operations of one turn apply atomically.** Either all accepted operations of a turn commit, or none do.

---

## 5. Authority model

There are no built-in roles. Authority is computed from records, declarations and earlier operations. A
benchmark without access rules declares one shared scope that every principal reads.

### 5.1 Sources of authority

1. **Speaking.** A speaker always holds full access to what they said.
2. **Ownership.** An owner of a record holds full access and may grant, restrict and restore it.
3. **Being a subject.** A principal a record is about holds full access by default and may grant it. The
   interpreter distinguishes *about* from *merely mentioned*; mentioned principals get nothing automatically.
   (Measured in gsm-rebuild: adding all mentioned people leaked; adding nobody cut medical U 0.695 → 0.486.)
4. **Declared relationships.** At reset, each relationship becomes a scope fact: the linked principals may read
   records whose subjects include the linked entity or principal, at the view the relationship's attributes
   allow. Attribute values are passed to the interpreter as text; the core does not interpret them.
5. **Operations.** Grants, restrictions, revocations and restorations applied earlier in the conversation.

### 5.2 Effective view

For a principal P and a record R at time t, the effective view is the most permissive view from sources 1–5,
then lowered by every applicable restriction, and set to none by every applicable denial. Denials outrank
everything except speaking. A tombstoned record has no view for anyone.

### 5.3 The denial ledger

A `revoke` writes a denial `(principal, record or scope, turn)`. A later widening for that principal on that
record or scope is accepted only if its source turn is newer than the denial **and** its authority is an owner
or subject. This keeps a revocation from being undone by a routine restatement.

### 5.4 Delegation

A relationship may declare that principal A acts for principal B. A's effective view is never wider than B's.
Records A writes on B's behalf list B as an owner. This is the hook for agents acting for humans.

### 5.5 Existence

A principal with no view of any responsive record must not learn whether one exists. The retriever reports
only counts to the release layer; the answerer is never told that something was withheld. Links and clusters
are visible only to principals who can read every record they connect (§8.4).

---

## 6. Data model (SQLite)

**The tables are fixed.** The memory's structure evolves as rows: notes, links, clusters, aliases, slots,
attributes, version chains and audiences. No table or column is created during a conversation. (shm_evolve's
live schema changes were measured: 23% of structure decisions rejected, 83% of proposals declined by its own
prompt, and no score gain traced to them.)

Content columns are sealed; everything else is metadata. Side tables start with `_`.

### 6.1 Tables

| Table | Columns (abridged) | Notes |
|---|---|---|
| `principals` | id, role, display_name, kind (`human`/`agent`/`unknown`), acts_for | from declarations |
| `entities` | id, kind_label | declared non-principals |
| `relationships` | id, type, endpoints (JSON), attributes (JSON) | verbatim from declarations |
| `records` | id, body (sealed), speaker_id, source_turn, created_at, valid_from, valid_to, superseded_by, slot, derived_from (JSON), tombstoned_at, kind | frozen envelope; body never rewritten |
| `record_subjects` | record_id, subject_id, is_owner | many per record |
| `record_views` | record_id, principal_id, view | mirrors key wraps |
| `record_redacted` | record_id, body (sealed) | redacted text, when one exists |
| `protected_values` | record_id, fingerprint | keyed hashes, never plaintext |
| `notes` | record_id, version, keywords (sealed), tags (sealed), context (sealed), status, reorg_id | append-only versions; the active one is used |
| `attributes` | record_id, name, type (`text`/`number`/`date`), value (sealed), status, reorg_id | typed values the organiser extracts; filterable at query time |
| `links` | a, b, kind, strength, status, reorg_id | §8.3 |
| `clusters` | id, label (sealed), status, reorg_id | |
| `cluster_members` | cluster_id, record_id, status, reorg_id | |
| `aliases` | alias, target_id, source (`declared`/`derived`/`interpreted`/`organised`), status, reorg_id | |
| `embeddings` | record_id, note_version, vector | purged on tombstone |
| `terms` | record_id, term_hash | relevance index, purged on tombstone |
| `_reorgs` | id, op, args (content-free), evidence (turn ids, span offsets), confidence, status, created_turn, decided_turn, reason | the reorganisation ledger (§8.5) |
| `_keys` | record_id, principal_id, wrapped_key | keyring, frozen |
| `_denials` | principal_id, target, turn | §5.3 |
| `_op_log` | seq, turn, op, args (content-free), authority, outcome, reason_code | append-only |
| `_signals` | seq, turn, kind, refs | §8.6 |
| `_health` | counter, value | §13.3 |

### 6.2 Note and organisation text is sealed under the record's key

A note's keywords, tags, context and attribute values are derived from its record and may restate it. They are
sealed with the record's key, so they open for exactly the record's audience and are destroyed with it.

### 6.3 Supersession

A new record supersedes an old one only when the slots match **and** the texts are similar above a calibrated
threshold (label-free: the p99 similarity of sentences from unrelated conversations). Two records from the same
turn never supersede each other. The new version keeps the old version's readers and their rights, unless the
turn narrows them. If a non-owner's correction would widen the audience, the old record is retired and the new
one is stored under its own audience.

### 6.4 Deletion

`delete` resolves its targets as the requester (governed search with the requester's identity), so nobody can
delete what they cannot see. It reaches every earlier version in the chain, every note version, every attribute,
link and cluster membership of the record. Values held only by deleted records are masked as `[removed]` in
records the same turn stores. With `parts`, only the named spans are removed and the remainder is re-stored with
the same subjects, views, denials and slot; its note is regenerated.

### 6.5 Derived records

A derived record's audience is the intersection of its sources' audiences, recomputed whenever a source's
audience shrinks. Deleting any source tombstones the derivation.

---

## 7. Ingestion

### 7.1 Pipeline per turn

1. **Resolve references** deterministically against the alias index (§7.3).
2. **Label** the turn with one model call (§7.2).
3. **Auxiliary calls**, only when the label asks for them: owners per quote; rescoping (consent or revocation
   about earlier records); deletion targets; supersession candidates; carry-forward parts.
4. **Build proposals** in the operation vocabulary. Trusted code, not the model, turns labels into operations.
5. **Validate**: schema, closed audience menu, quotes verbatim, protected values present in the turn.
6. **Authorise** each operation (§5). Tightening first, then everything else in turn order.
7. **Apply** atomically; compute integrity signals; update health counters.
8. **Organise** the new records (§8).

### 7.2 The label call

Input: the roster (principals with display names and roles, relationships with attributes), up to 12 recent
standing instructions, the previous 3 turns (deletion requests replaced by a placeholder), the slots the speaker
can currently read, and the turn.

Output, schema-validated JSON:

| Field | Meaning |
|---|---|
| `kind` | `fact`, `instruction`, `deletion`, `correction`, `question`, `chatter` |
| `quotes` | at most 6 verbatim spans, one per independent piece of information; each with optional `slot` and optional `shareable` (redacted) text |
| `about` | subjects per quote, from the closed menu |
| `audience` | full audience and redacted audience, from the closed menu |
| `exclude` | principals the turn says must not be told |
| `protected_values` | exact sensitive values in the turn |
| `targets` | for deletions and corrections: what earlier information is meant |

The closed menu is: this conversation's principal ids, entity ids, relationship groups, role labels,
`speaker`, `about`, `everyone`. Unknown tokens are dropped and counted.

Trusted rules applied to the label:

- **Completeness guard.** If a quote is not verbatim, or a protected value or number of the turn is in no quote,
  the whole turn is also stored.
- **Privacy floor.** If the turn has protected values or privacy cues, `everyone` is removed from the full
  audience; a redacted text that repeats a protected value is discarded.
- **Questions are not facts.** A turn that only asks stores nothing; a quote that is itself a question is dropped.
- **Exclusions win.** A principal in `exclude` gets an explicit denial on the turn's records.

### 7.3 Reference resolution

Turns refer to people and things by name, first name or shortened name, almost never by id.

- **Declared aliases**: each display name, its first and last token.
- **Derived aliases**: tokens of an entity or principal id that are distinctive within the conversation.
- **Interpreted and organised aliases**: proposed by a model; accepted only when the alias occurs in a turn and
  the target is in the roster; subject to the reorganisation lifecycle (§8.5).
- **Matching** is word-boundary, longest match first, so a name nested inside a longer name never resolves to
  the shorter one.

### 7.4 Model calls

- One OpenAI-compatible client, configured per call type (`label`, `aux`, `organise`, `consolidate`,
  `query_split`). Any model; no model is trained.
- Thinking off by default; configurable per call type.
- Every reply is parsed against a JSON schema. On failure: one retry with the parse error appended; then the
  deterministic fallback for that call; the fallback is counted.
- Responses are cached by prompt hash, per run.
- A per-conversation budget (calls and tokens). Budget refusals are a health metric (§13.3).

---

## 8. Self-organisation within a conversation

This is the memory evolving its own structure while a conversation is in progress. It follows A-Mem's three
steps (enrich each new note, link it to its neighbours, evolve the neighbours) and changes them in four ways:
records are never rewritten, organisation never moves information to people who may not see it, every
reorganisation carries checkable evidence and a status, and later turns confirm or retract earlier
reorganisations.

### 8.1 What A-Mem does, for reference

For each new memory A-Mem has a model write keywords, tags and a context description; finds the 10 nearest
notes by embedding; has a model choose links among them; and has a model rewrite each neighbour's context,
keywords and tags in place. There is no validation and no history. Ablation on LoCoMo multi-hop (F1, GPT-4o-mini):
9.65 without links or evolution, 21.35 with links only, 27.02 with both.

### 8.2 Two classes of reorganisation

| Class | Operations | Cost of a mistake | Regime |
|---|---|---|---|
| **Organisation** | `annotate`, `link`, `cluster`, `alias` used only for ranking, attributes | some utility; never a leak | applied immediately as `active`; retracted on contradiction |
| **Governance** | audience changes (`grant`, `restrict`, `revoke`, `share_scope`), `assign_slot`/`supersede`, `delete`, aliases that decide audiences | a leak (widening) or lost information (deletion, wrong supersession) | asymmetric rules (§8.5) |

### 8.3 The organisation steps

After the records of a turn are applied:

1. **Enrich.** One `organise` call per new record writes its note: keywords, tags, a one-sentence context, and
   typed attributes (dates, amounts, quantities, statuses) with their verbatim source spans. An embedding is
   computed over the record text and its note.
2. **Find neighbours.** The `k` nearest records (default 10) by embedding plus lexical overlap, restricted to
   current, untombstoned records the organiser may maintain.
3. **Link.** The model chooses links among the neighbours from a closed set of kinds: `same_topic`,
   `same_value_slot`, `follow_up`, `refers_to`, `contradicts`, `part_of`. A `same_value_slot` link becomes an
   `assign_slot` proposal and enters the governance regime.
4. **Evolve neighbours.** For each neighbour the model may propose a new note version (context, keywords, tags,
   attributes), seeing the new record and the neighbour. The old note version is kept; the new one becomes active.
5. **Cluster.** Every `CLUSTER_EVERY` turns (default 10), a `consolidate` call groups linked records into named
   clusters and reviews pending reorganisations (§8.7).

### 8.4 Audience rules for organisation

Organisation is a channel through which information could move between audiences. These rules close it:

- **A note may only be written from sources its readers can read.** A neighbour's new note version may use
  information from the new record only if the neighbour's audience is a subset of the new record's audience.
  Otherwise the proposal is rejected (`AUDIENCE_WIDENS`); if the connection is worth keeping, a derived record
  is created instead, with the intersection audience (§6.5).
- **A link or cluster membership is visible only to principals who can read both ends** (every member, for a
  cluster). Link expansion at query time follows only links the asker can see.
- **A cluster label is sealed under the intersection of its members' audiences.**
- **The organiser never proposes audience changes.** Only the interpreter does, from instructions in turns, under
  §5. A `same_value_slot` link can cause supersession only through `supersede`'s own authority rule.
- **Deletion removes organisation.** Every note version, attribute, link and cluster membership of a deleted
  record is destroyed with it; a cluster label derived from it is regenerated or destroyed.

### 8.5 The reorganisation lifecycle

Every reorganisation is a row in `_reorgs` with status `pending`, `active` or `retracted`.

| Proposal | Initial status | Becomes `active` when |
|---|---|---|
| organisation (note, link, cluster, ranking alias, attribute) | `active` | immediately |
| tightening (restrict, revoke, exclusion) | `active` | immediately |
| widening (grant, share_scope, an alias that would widen an audience) | `pending` | its authority is valid **and** its evidence checks **and** a second independent check agrees (§8.6) |
| supersession from organisation | `pending` | slot and similarity rules of §6.3 hold **and** no `contradicts` link points at it |
| deletion | applied immediately by `delete`'s own rules | — |

A `pending` reorganisation has no effect on access or on what is current. A `retracted` one has its effect undone:
a note reverts to its previous version, a link or membership disappears, a pending widening is dropped. Retraction
never undoes a deletion and never re-widens past a denial.

### 8.6 Knowing whether a reorganisation is right, without feedback

At runtime nothing tells the memory whether one reorganisation is correct. Correctness is approached in four ways.

1. **Hard checks.** Deterministic, always applied: authority, closed vocabularies, verbatim evidence, the audience
   rules of §8.4, invariants. A proposal that fails is rejected outright.
2. **Evidence anchoring.** Every reorganisation names the turn spans that justify it (an alias names turns where the
   name occurs; an attribute names its source span; a slot assignment names both values). Code checks the spans
   exist. No evidence, no reorganisation.
3. **Agreement for widening.** A widening becomes active only if a second, independent check agrees: a second
   sample with a different phrasing of the same question, or a different call type reaching the same conclusion.
   Disagreement leaves it pending.
4. **Delayed evidence from the conversation itself.** Later turns confirm or contradict earlier decisions, and are
   recorded as signals in `_signals`:

| Signal | Suggests |
|---|---|
| a later turn restates the new value of a slot | the supersession was right (confirm) |
| two current records in one slot differ | a missed supersession or a wrong slot (review) |
| a name keeps co-occurring with the principals linked to its alias target | the alias holds (confirm) |
| a name never resolves | a missing alias (propose) |
| a later turn reaffirms a consent ("as I said, she can see it") | the grant was right (confirm) |
| a later turn objects that someone saw something | a widening was wrong (retract and restrict) |
| a `contradicts` link appears on a record in a slot | the supersession is suspect (review) |
| a declared member's queries keep returning nothing or only denials | scoping or aliases may be wrong (review; never widen on this alone) |

Read-time signals are written by `query` to `_signals` only; they never change access during the query.

### 8.7 Consolidation

Every `CLUSTER_EVERY` turns and before the first query after new turns, the `consolidate` step:

- promotes pending reorganisations whose confirmation conditions now hold;
- retracts reorganisations contradicted by later evidence;
- re-examines records named by `review` signals, producing new proposals under the same rules;
- regroups clusters.

Consolidation is the only place where the memory revisits its own earlier decisions. Everything it proposes passes
through the core like any other proposal.

### 8.8 Measuring correctness offline

Whether the self-organisation policy is right on average is measured, not assumed:

- **Synthetic conversations** with known correct reorganisations (consent, revocation, supersession, reference by
  name, nested names, attribute values), in two paraphrases and two unseen vocabularies, give precision and recall
  per reorganisation type. These are tests, not training data.
- **Benchmark scores with ablations** (§12.6): no organisation; enrichment only; plus links; plus neighbour
  evolution; plus consolidation; pending versus immediate widening.

### 8.9 Worked example: one turn's reorganisation as rows

The memory holds two graphs in the fixed tables, and reorganising either one means inserting rows and switching
which version is active.

| Graph | Nodes | Edges | Tables |
|---|---|---|---|
| **Note graph** (A-Mem style, for ranking) | records with their active notes | typed links; cluster membership | `notes`, `links`, `clusters`, `cluster_members`, `embeddings` |
| **Entity-fact graph** (for current values and as-of questions) | principals and entities | record-to-subject edges; typed facts; slots as `(subject, fact name)` with versions | `record_subjects`, `attributes`, `records.slot`, `valid_from`/`valid_to` |

A-Mem has only the first. The second is a light temporal knowledge graph: supersession is a new version of a fact
with validity dates.

Note versions carry `status` ∈ {`active`, `previous`, `retracted`}; exactly one version per record is `active`.

**Setup.** The memory already holds record `r12`, "Kestrel launches 19 August", in slot `proj_k:launch_date`.
Turn `t040`, from principal `p3`, says "Kestrel's launch moves from 19 August to 2 September." `p3` is in `r12`'s
full audience, and every reader of `r12` is in the new record's audience.

```sql
-- 1. Interpreter: the new record is a verbatim span; the label call assigned subject and slot.
INSERT INTO records(id, body, speaker_id, source_turn, slot, created_at, valid_from, kind)
  VALUES ('r57', <sealed>, 'p3', 't040', 'proj_k:launch_date', <ts>, <ts>, 'correction');
INSERT INTO record_subjects(record_id, subject_id, is_owner) VALUES ('r57', 'proj_k', 0);

-- 2. Interpreter: supersede passes §6.3 (same slot, similar text, p3 may revise r12).
--    Only metadata changes on r12; its body is never rewritten (I-9). r57 keeps r12's readers.
UPDATE records SET valid_to = <ts>, superseded_by = 'r57' WHERE id = 'r12';

-- 3. Organiser, enrich: note and typed fact for r57, each fact anchored to its source span.
INSERT INTO _reorgs(id, op, args, evidence, confidence, status, created_turn)
  VALUES ('g90', 'annotate', '{"record":"r57"}', '{"turns":["t040"],"spans":[[28,39]]}', 0.95, 'active', 't040');
INSERT INTO notes(record_id, version, keywords, tags, context, status, reorg_id)
  VALUES ('r57', 1, <sealed>, <sealed>, <sealed>, 'active', 'g90');
INSERT INTO attributes(record_id, name, type, value, status, reorg_id)
  VALUES ('r57', 'launch_date', 'date', <sealed '2026-09-02'>, 'active', 'g90');

-- 4. Organiser, link: the new record and its neighbour share a slot.
INSERT INTO _reorgs VALUES ('g91', 'link', '{"a":"r57","b":"r12","kind":"same_value_slot"}',
  '{"turns":["t040"]}', 0.9, 'active', 't040', NULL, NULL);
INSERT INTO links(a, b, kind, strength, status, reorg_id) VALUES ('r57', 'r12', 'same_value_slot', 0.9, 'active', 'g91');

-- 5. Organiser, evolve the neighbour: allowed by §8.4 because r12's audience ⊆ r57's audience.
--    The old note version is kept as 'previous'.
INSERT INTO _reorgs VALUES ('g92', 'annotate', '{"record":"r12"}', '{"turns":["t040"]}', 0.85, 'active', 't040', NULL, NULL);
UPDATE notes SET status = 'previous' WHERE record_id = 'r12' AND status = 'active';
INSERT INTO notes(record_id, version, keywords, tags, context, status, reorg_id)
  VALUES ('r12', 2, <sealed>, <sealed>, <sealed context: "earlier launch date, replaced at t040">, 'active', 'g92');
```

All five steps commit atomically with the turn. If `r12` had a reader outside `r57`'s audience, step 5 would be
rejected with `AUDIENCE_WIDENS` and logged, and `r12`'s note would stay as it was.

**Retraction.** Retracting `g92` sets `r12`'s version 2 to `retracted` and version 1 back to `active`. Retracting
`g91` sets the link to `retracted`. The ledger keeps both decisions and their reasons. Retraction never touches a
deletion or a denial.

**Query time.** Link expansion (§9.1 step 3) is a recursive query that follows only active links the asker can see:

```sql
WITH RECURSIVE hop(id, depth) AS (
  SELECT id, 0 FROM seed_candidates
  UNION
  SELECT CASE WHEN l.a = hop.id THEN l.b ELSE l.a END, depth + 1
  FROM links l JOIN hop ON hop.id IN (l.a, l.b)
  WHERE l.status = 'active' AND hop.depth < :max_depth
    AND visible_to(:asker, l.a) AND visible_to(:asker, l.b)
)
SELECT DISTINCT id FROM hop;
```

`visible_to` is a join on `record_views` for the asker, not a model call. Expanded records still pass the gate
(§9.1 step 4) before anything is released. An as-of question about the launch date reads the slot's version chain
and returns `r12` before `t040` and `r57` from `t040` on.

**Embeddings at this scale.** A conversation holds hundreds to a few thousand records, so nearest-neighbour search
is brute-force cosine over vectors stored as blobs, computed in Python. If the continuous setting or a large
LongMemEval haystack outgrows that, the `sqlite-vec` extension adds a vector index inside the same file.

**Why rows and not new tables.** The model may propose any keyword, tag, link, cluster or fact name. Queries never
break, because the shape of the data never changes. Every change is checkable, sealed under the right audience,
and reversible.

---

## 9. Retrieval and disclosure

### 9.1 Steps for a query `(asker, text, as_of)`

1. **Candidates**: records with `created_at ≤ as_of`, not tombstoned, current at `as_of`. Superseded versions are
   candidates only for principals who may read history (owners and speakers).
2. **Relevance**: lexical scoring over the term index plus embedding similarity over record and active note.
   Attribute filters apply when the question names a typed value. Irrelevant records drop out here, before any
   policy check.
3. **Link expansion**: add records linked to the top candidates by links the asker can see, and members of their
   clusters, up to a cap.
4. **Gate**: open each candidate with the asker's identity. Records that do not open are counted, never shown.
5. **Projection**: release each opened record at the asker's effective view; `redacted` releases the redacted
   text; `existence` releases nothing but counts toward the suggested action.
6. **Budget**: if every opened record fits the answerer's context budget, release all of them, oldest first.
   Otherwise rank, favouring the current thread (same subjects or cluster as the question), and split multi-part
   questions into sub-queries merged round-robin.
7. **Attribution**: every released record carries its speaker's principal id and role.
8. **Suggested action**: `answer` if any full-view record answers; `answer_redacted` if only redacted ones do;
   `refuse` if responsive records exist and none opened; `no_memory` otherwise.

### 9.2 Budget

The budget is expressed in answerer tokens: the answerer's context minus its maximum output minus the rendered
prompt minus a margin. GSM's 400 records / 60,000 characters is the starting point for comparison.

### 9.3 Release check

Applied when the benchmark's answerer writes the answer:

- Declining (`refuse`, `no_memory`) is always accepted.
- `answer` and `answer_redacted` are accepted only if evidence was released, the answer cites no unreleased record,
  and no protected-value fingerprint of an unreleased record appears in it.
- On failure, fall back to a deterministic rendering of the released evidence; with no evidence, `no_memory`.

---

## 10. Benchmark framework

### 10.1 What a benchmark provides

Every benchmark is mapped onto the same four things:

| Concept | Meaning |
|---|---|
| **declarations** | principals, entities, relationships at the start of a conversation |
| **stream** | turns and session markers, in order |
| **probes** | queries at points in the stream: asker, text, as-of position |
| **scoring** | the benchmark's own metric, applied to predictions |

### 10.2 Adapter interface

```python
class BenchmarkAdapter(Protocol):
    name: str
    def conversations(self, split: str) -> Iterable[Conversation]   # declarations + stream + probes, annotations stripped
    def render_answer(self, probe, evidence, suggested_action) -> Prediction   # the benchmark's answerer and prompt
    def score(self, predictions, split: str) -> Report                # the benchmark's own scorer, plus our judge
    def baselines(self) -> dict[str, Path]                            # existing baseline predictions, rescored on our judge
```

Annotations never enter `Conversation`; a canary test plants unique strings in every annotation field and asserts
none reaches any memory component or the database.

### 10.3 Planned adapters

| Benchmark | Declarations | Access rules | Answerer | Notes |
|---|---|---|---|---|
| **GateMem** (first) | episode principals and relationships | yes | the benchmark's own, Qwen3.8-27B | primary target, all four domains |
| **LongMemEval** | user and assistant | none: one shared scope | Qwen3.8-27B with its reference answer prompt | knowledge-update, temporal, abstention and multi-session subsets on the small haystack; the medium haystack as a retrieval stress test |
| **LoCoMo** | two speakers | none: one shared scope | Qwen3.8-27B with its reference answer prompt | checks that organisation and governance do not cost plain recall |

For benchmarks without access rules the governance machinery still runs; it simply never denies.

---

## 11. Interfaces

### 11.1 Library

```python
mem = Memory.open(path, models=ModelConfig(...), features=Features(...))
mem.declare(principals=[...], entities=[...], relationships=[...])
mem.ingest(Turn(turn_id, timestamp, speaker_id, text))           # returns applied, pending and rejected operations
mem.apply(op, authority=principal_id)                             # direct typed operation by a caller
mem.query(asker_id, text, as_of=None) -> Evidence                 # released records + suggested action + counters
mem.history(record_id, asker_id)                                  # versions the asker may see
mem.reorganisations(since_seq)                                    # the reorganisation ledger, content-free
mem.audit(since_seq)                                              # op log, content-free
```

`Features` switches every mechanism on or off, which is how ablations run.

### 11.2 GateMem adapter

Implements the fixed benchmark interface `GateMemAgent(db_path)`, `reset(episode)`, `ingest(turn)`,
`query(checkpoint)` returning `action`, `answer`, `used_record_ids`, `evidence`, `debug`.

### 11.3 Agent tool server (last)

The same library exposed as tools (`remember`, `recall`, `share`, `revoke`, `forget`, `history`). The calling agent's
identity comes from the server session, never from tool arguments.

### 11.4 Concurrency

v1 serialises writes per memory with one writer lock. Each operation names the record versions it read; if any
changed before commit, the operation is rejected with `STALE_READ`.

---

## 12. Evaluation protocol

### 12.1 Generality check instead of tuning on everything

There is no training and no outer loop, but people still tune by hand from failures. To keep a claim of
generality honest:

- **GateMem**: design and diagnose on three domains; score the fourth once, at the milestone report. Which domain is
  held out is fixed at M0 (recommended: household, the domain least used in earlier diagnosis).
- **Other benchmarks** are not used for diagnosis until GateMem results are frozen; their first run is reported as is.

### 12.2 Settings

- **Per-conversation**: a fresh memory per episode or question history.
- **Continuous** (GateMem): one memory per domain, never reset, episodes in file order.

### 12.3 What is measured

- GateMem: U, A, F, OR and MGS, answer-level and strict, per domain and as the four-domain mean.
- Other benchmarks: their own metric, per category.
- Organisation: precision and recall per reorganisation type on synthetic conversations (§8.8); counts of active,
  pending and retracted reorganisations per conversation.

### 12.4 Targets on GateMem

- **Primary**: mean answer-level MGS.
- **Floor**: mean strict MGS ≥ 0.30 (GSM v20auth: 0.299); no domain's strict MGS below v20auth's minus noise.
- **Bar**: beat GSM v20auth in every domain by more than the measured noise (medical 0.624, office 0.613,
  education 0.426, household 0.490). **Stretch**: mean answer-level ≥ long_context's 0.624.

### 12.5 Noise and comparisons

- Before any comparison, run the same configuration three times and record the spread per metric and domain.
- Compare configurations by pairing per probe and reporting a bootstrap 95% interval on the difference. A change
  counts as an improvement only if the lower bound is above zero.

### 12.6 Ablations

Each mechanism is switched off alone from the full system: reference resolution, per-quote owners, keep_readers,
carry_forward, partial delete, history deletion, the denial ledger, the completeness guard, relevance before policy,
full-context budget, thread-first ranking, query splitting, note enrichment, linking, link expansion, neighbour
evolution, clustering, consolidation, pending versus immediate widening, delayed-evidence signals, release check,
and memory-model size (Qwen3-4B versus Qwen3-8B, used as-is).

### 12.7 Cost

Every run prints expected and actual model calls and tokens per call type. Runs are resumable per conversation.

---

## 13. Invariants, tests and health

### 13.1 Invariants (each is a test)

| # | Invariant |
|---|---|
| I-1 | Every text in evidence was opened with the asker's identity. |
| I-2 | No grant, widening or revocation happens during `query`. |
| I-3 | `query` changes no access state; only `_signals` and `_health` may change. |
| I-4 | An operation without valid authority is rejected and changes nothing. |
| I-5 | A denial is not undone by a later widening unless §5.3 allows it. |
| I-6 | After `delete`, no later query releases, restates or confirms the content, and no file under the memory's directory contains its plaintext, including in notes, attributes and cluster labels. |
| I-7 | Deleting a record deletes every version in its chain, every derivation, and all its organisation rows. |
| I-8 | A derived record is never readable by someone who cannot read every source. |
| I-9 | Stored record text is a verbatim span of its source turn and is never rewritten. |
| I-10 | A record whose slot was superseded is not released as current. |
| I-11 | A note never holds information from a record outside the note's own audience (§8.4). |
| I-12 | A link, cluster membership or cluster label is never visible to a principal who cannot read every record it connects. |
| I-13 | A pending reorganisation has no effect on access or on what is current. |
| I-14 | No dataset vocabulary appears in code or prompts. |
| I-15 | No annotation field reaches the library (canary test). |
| I-16 | With every model disabled, every component falls back deterministically and the contract tests still pass. |

### 13.2 Test suites

- **Unit and property tests** for `core`, with no model.
- **Contract tests** through the public interface on synthetic conversations with meaningless names.
- **Reorganisation tests**: synthetic conversations with known correct reorganisations (§8.8), run with the model
  enabled; every family must pass at least once, and each check is verified to fail on a memory lacking the
  mechanism.
- **Fake model** for deterministic end-to-end tests of every model path.

### 13.3 Health metrics

Reported per run; thresholds halt the run or mark it ineligible.

| Metric | Threshold |
|---|---|
| model reply parse failures after retry, per call type | ≤ 1% |
| deterministic fallbacks, per call type | ≤ 2% |
| budget refusals | 0 |
| organisation proposals rejected by §8.4 | reported; investigate above 10% |
| answerer failures or empty replies | ≤ 0.5% |
| judge falling back to the rule scorer | 0 |
| conversation errors and timeouts | 0 |

---

## 14. Models and serving

| Role | Model | Serving | Notes |
|---|---|---|---|
| Answerer | Qwen3.8-27B | SoCLaaS `qwen3.8:27b`, or local vLLM | thinking ON |
| Judge | Qwen3.8-27B | same | baselines rescored with the same judge |
| Memory model (interpreter and organiser) | Qwen3-8B (default), Qwen3-4B (cheap); any model allowed | local vLLM on a free H200 (GPU 7 at time of writing) | used as-is; fallback SoCLaaS `qwen3.5:9b` |
| Embeddings | bge-m3 or all-MiniLM-L6-v2 | SoCLaaS `bge-m3`, or local | pretrained, not tuned; lexical-only must still work |

On SoCLaaS the slug `qwen3.8:27b` has no vendor prefix; the thinking switch must match it explicitly. Keep
concurrency under 20 requests and respect the $50/day cap.

---

## 15. Package layout

```
govmem/
  SPEC.md  README.md  pyproject.toml
  src/govmem/
    core/        schema.py keyring.py ops.py authority.py lifecycle.py store.py reorgs.py signals.py health.py
    interpret/   client.py label.py aux.py aliases.py proposals.py prompts/*.md fallback.py
    organize/    enrich.py neighbours.py link.py evolve.py cluster.py consolidate.py audience.py prompts/*.md
    retrieve/    candidates.py relevance.py expand.py gate.py project.py budget.py action.py
    release/     check.py
    framework/   adapter.py runner.py features.py cost.py
    api.py       Memory, Turn, Evidence, ModelConfig, Features
  adapters/      gatemem/ longmemeval/ locomo/
  eval/          judge.py scores.py strict.py noise.py compare.py ablate.py
  tests/         core/ contract/ reorg/ invariants/ lint/ adapters/
```

---

## 16. Milestones

| Milestone | Delivers | Exit criteria |
|---|---|---|
| **M0 Spec** | this document, agreed | user sign-off; §17 answered |
| **M1 Core** | schema, keyring, operations, authority, lifecycle, reorganisation ledger | I-1 to I-13 pass with no model |
| **M2 Interpreter** | label and auxiliary calls, aliases, proposals, fallbacks | contract tests pass with and without model; health within thresholds on 2 dev episodes per GateMem domain |
| **M3 Organiser** | enrichment, linking, neighbour evolution, clustering, consolidation, signals | reorganisation tests pass every family; I-11, I-12 pass |
| **M4 Retrieval** | candidates, relevance, link expansion, gate, projection, budget, release check | GateMem adapter runs one dev episode per domain end to end; canary test passes |
| **M5 Framework and eval** | adapter interface, runner, judge, strict scorer, noise, comparisons, cost | noise floor measured; GateMem baselines rescored on our judge |
| **M6 GateMem results** | three dev domains, per-conversation | beats GSM v20auth on dev domains beyond noise, strict floor held |
| **M7 GateMem report** | held-out domain, continuous setting, ablations | written report with intervals |
| **M8 Other benchmarks** | LongMemEval and LoCoMo adapters, first runs | reported as is |
| **M9 Agent server** | tool server over the library | after M8 |

---

## 17. Open questions

1. **Held-out GateMem domain.** Household is recommended (§12.1). Confirm, or choose another.
2. **Answerer serving.** SoCLaaS costs money per run; local vLLM needs a second free GPU or sharing GPU 7 with the
   memory model. Decide before M5.
3. **Organisation budget.** Enrichment, linking and neighbour evolution add model calls per record. A-Mem reports
   about 1,200 tokens per operation. Is a per-conversation cap of about 2× the interpreter's cost acceptable?
4. **Relationship attributes.** Scope strings are passed to the interpreter as text. Should the core also support a
   structured, topic-limited view in v1?
5. **Redacted text.** Written by the label call (as in GSM), or by a separate call per restricted audience?

---

## 18. Implementation notes (first build, 2026-10-16)

Where the code and this document differ. The code is authoritative for these points until the spec is revised.

- **Layout.** `adapters/` and `eval/` live inside the package (`src/govmem/adapters`, `src/govmem/eval`).
- **Owners per quote** are reported by the label call itself, not by a separate auxiliary call (§7.1 step 3).
- **Unqualified relationships.** A declared relationship with no attributes gives each principal endpoint full
  access to records whose subjects include any other endpoint. Relationships with attributes give nothing
  automatically; the interpreter reads their attributes as text (§5.1 source 4).
- **Supersession** additionally requires that both records hold a value-like token (number, date, time, code)
  unless the new record comes from a turn labelled `correction`. The first live turns showed model slot keys
  grouping an instruction with a value.
- **Not built yet:** `carry_forward`; re-interpretation triggered by read-time signals (signals are logged
  only); the agent tool server (M9); the LongMemEval and LoCoMo adapters (M8); noise measurement and paired
  comparisons (M5).
- **Organiser.** One `organise` call per turn covers enrichment, linking and neighbour evolution for all of the
  turn's new records. §8.4 is enforced by construction: the existing records shown are only those whose readers
  include every reader of the new records, and neighbour updates pass the core's `AUDIENCE_WIDENS` check.
- **Consolidation** is deterministic (no model call): it promotes or retracts pending `same_value_slot` links
  (promotion supersedes only with identical subjects and cosine ≥ 0.6), logs slots with several current records,
  and rebuilds clusters as connected components of active links. Cluster labels are sealed under an OWNER-only key.
- **Widening agreement** (§8.5) is a second call (`rescope_check`) with a different phrasing and reversed record
  order; only records both calls choose are granted, the rest are logged as pending grants.
- **Release check** runs after the answerer against the memory's end-of-episode state.
- **Embeddings**: all-MiniLM-L6-v2 on CPU; a hashing fallback when it is unavailable (used by the tests).
- **Deletion reaches copies and later restatements** (added after the first smoke run). `delete` (a) re-stores
  with values masked any other record the requester can see that repeats a removed specific value (4+ digits) or
  a removed text verbatim, and (b) records keyed hashes of removed specific values, capitalised names and 5-word
  runs in `_forgotten` (a fixed table); anything stored later that repeats one is masked as `[removed]`.
  Reason: in the smoke run, confirmations written after a deletion restated the deleted numbers and names.
- **Release check masks, never replaces.** A protected value of an unreleased record in the answer is masked as
  `[withheld]`; the first version replaced the whole answer with the evidence and lost correct answers.
- **Continuous setting** (`run --continuous`): episodes merged in file order into one conversation, turn ids
  prefixed `eNN:`, principals and relationships unioned (first occurrence of a recurring id kept).
- **Roster window** (`GOVMEM_ROSTER_WINDOW`, turns; 0 = off, the per-episode default). In a never-reset memory the
  menu shown to the model, and the people `everyone`, `role:`, `group:` and declared-thing tokens resolve to, are
  restricted to principals who spoke or were named in the last N turns. Principals named by id still resolve.
  Without it, a role token in one conversation reached every holder of that role in the domain (242 principals
  in medical).
- **LoCoMo adapter** (`adapters/locomo`, M8 started 2026-10-17): two speakers plus a neutral `questioner`;
  `Features.open_access` makes every principal read every record (the gate still runs and never denies). Turns
  carry their session date as the timestamp, shared photos become `[shares a photo: <caption>]`, and released
  evidence items carry `timestamp` and `source_turn`. Category 5 (adversarial) is excluded. Answer prompt and judge
  are ours (A-Mem/Mem0 style), shared with the full-context baseline; F1 is LoCoMo-style without stemming.
