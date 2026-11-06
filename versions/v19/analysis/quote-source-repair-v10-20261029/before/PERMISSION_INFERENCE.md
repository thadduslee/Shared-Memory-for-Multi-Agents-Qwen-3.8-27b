# Ingestion-time permission inference

For version 7's candidate/approval interfaces, recipient coverage, erasure
semantics, performance changes, and validation, see
[Governed authorization](GOVERNED_AUTHORIZATION.md). The v6 observations below
describe the retained earlier design and diagnostics; v6 run artifacts are not
updated in place.

`agentic_memory/privacy/permissions.py` implements a domain-independent permission
updater. The LLM interprets involvement, authority statements, restrictions and
information necessity. The host validates its structured proposal and compiles
accepted bindings into exact resources enforced by `PrivacyMemorySystem`.
Ordinary memory evolution still cannot change permissions.

## Per-turn deltas and deferred enrichment (version 10)

The default GateMem runner now records permission version `10` and prompt
`permission-inference-v10-delta`. Start a fresh run directory. The initial
participant roster and requester grants remain empty; authenticated speakers,
their activities and narrow information permissions are learned as before.

- Control changes are staged before validating information bindings. A binding
  must assess every restriction in the **final** proposal, including newly
  created restrictions, irrespective of operation order. Missing assessments
  produce an operation path and explicit missing/unexpected restriction IDs.
  Revoked/suspended assignments cannot receive bindings earlier in the response
  to evade their final state. The complete update still publishes atomically.
  The top-level `considered_restrictions` acknowledgment must name every stored
  restriction and may also name restrictions created by valid operations in
  that response. Unknown or duplicate IDs remain invalid. Each binding's
  `restriction_decisions` must independently cover the complete final set.
- Invalid proposals have a durable three-rejection budget per job, shared by
  `run`, `update`, reconstruction and automatic recovery. A rejected job remains
  pending for access enforcement. Recovery records `state: blocked` and exits
  after exhaustion. A fresh process cannot silently reset this budget. Transient
  transport failures do not consume it. Invalid/truncated model responses have
  bounded contract retries; permanent HTTP request/configuration errors stop
  immediately. Inspect the saved validation and use an explicitly reviewed
  repair/version rather than deleting audit records or repeatedly restarting.
- Real-model `compact-delta-v1` responses reuse existing assignment IDs and
  supply only new bindings or actual control changes. A binding supplies
  `necessity`, `scope_supported`, `context_preserved`, `metadata_safe`, evidence
  for necessity/subject/original authorization, exact selected text, evidence
  kind and explicit final-state restriction decisions. The host fills duplicate
  explanation/task-necessity/source-ID bookkeeping; it never fabricates scope,
  authority or restriction applicability. Recipient review is a compact object
  `{reviewed, evidence, unresolved}` instead of repeated unchanged explanations.
  Every required recipient and live supporting evidence remain mandatory.
  Canonical ledger records retain the original enforcement format.
- Field text, embeddings and access edges still commit each turn. Optional tags
  and related-memory links go into `evolution_queue` in the existing memory
  database. The runner serves and scores the boundary's questions first, then
  drains at most one batch of four fields. At invocation end it flushes remaining
  partial batches. This is scheduled batch work, not a concurrent background
  thread. Direct adapter callers use `flush_evolution(processor, flush=True)`
  to drain their queue explicitly. Transient enrichment failures leave work
  queued while later permission updates can proceed. Invalid enrichment is
  rejected; deleted, superseded or inactive-grant fields are discarded. Claimed
  batches retain exact job/input IDs across restart, and core source/processing
  checks still bracket model execution and publication.

The manifest records `evolution_schedule: after-answers-durable-batches-of-4`.
Summary `evolution.queue` exposes pending/complete/stale/rejected items separately;
`evolution.enrichment_complete` indicates whether queued work remains. Evaluation
coverage does not claim successful enrichment when optional work is pending.
Existing v9 databases and predictions are preserved and cannot be silently
resumed under the new prompt/schedule. The v9 context selection below is retained.

See [validation and the read-only t005 replay](analysis/per-turn-fixes-v10-20261029/VALIDATION.md).

## Incremental context (version 9)

Permission inference now sends a selected context rather than replaying the
entire conversation prefix. `permission_context.py` builds the model projection;
the canonical ledger, original scope evidence, exact bindings and source history
remain in the host stores.

The initial context includes:

- The newest six live turns, including the new event.
- Current activities and assignments related to recent speakers/subjects, and
  other recipients of those subjects. Routing is a conservative candidate
  selection, not a permission decision.
- Pending involvement, unresolved decisions and proposed assignments, even when
  their participants have left the recent window.
- Original authorization, corroboration and condition evidence for loaded
  records, with one adjacent turn on either side to interpret short acceptances.
  Scope text, validity intervals and event-based expiry remain intact.
- Every restriction. Restriction applicability is still checked globally;
  filtering denials by a generated scope label would lose semantic constraints.
- Two recent bindings per loaded assignment, with `binding_count` indicating
  the complete retained history. Omitted bindings are retained by the host.
- A compact `context.ledger_index` of current record IDs, participants, subjects,
  activities, scopes, statuses and validity, for discovering other affected
  permissions. Index entries cannot stand in for authorization evidence.

For an ordinary appointment-time change, the learner can reuse an existing
assignment and emit only `bind_information`, citing its original authorization
and the new fact. No mandatory routing model call or full-history pass is added.
No operation means retain the existing permission.

When an antecedent or old fact is missing, the learner can instead return:

```json
{
  "revision": 12,
  "considered_restrictions": [],
  "operations": [],
  "context_requests": [
    {"kind": "search", "reference": "sister appointment times", "offset": 0}
  ]
}
```

`source` loads a live source by ID and its immediate neighbors. `search` uses
case-insensitive token overlap over source text, authenticated speaker and event
ID, ranking by overlap then recency. It returns six matches per page with
neighbors; this is lexical retrieval, not semantic or exhaustive recall.
`activity` and `assignment` load a specific control record with its evidence.
`bindings` pages through six prior bindings of an assignment, loading their source
text and evidence. `reference` is the relevant source/ledger ID or search words;
`offset` is zero except for search/binding pagination. Results report matched
source IDs and total match counts. Multiple requests accumulate in the context.

There are at most four requests per round and three retrieval rounds per
unchanged snapshot. Retrieval responses cannot contain mutations; coverage may
be empty during retrieval but is checked normally for the final proposal.
Each round is a separately audited model call. Accepted requests survive
processor failures and updater reconstruction through `permission_attempts`;
retrying cannot reset the budget. A changed publication snapshot rebuilds context
and invalidates the old retrieval session. Budget exhaustion leaves the job
pending/rejected and requester access blocked until the host resolves it.

Source retrieval is limited to the job's immutable causal prefix, current live
versions and compartment. Deleted, superseded, future and other-compartment
sources cannot be fetched. A final operation citing evidence or touching an
existing activity/assignment outside its loaded context is rejected with the
required retrieval request. Source aliases are expanded before these checks;
literal search words are preserved.

The host validates the final delta against the **complete** canonical state and
live prefix. This preserves earlier bindings, independent restrictions,
cross-recipient effects, erasure checks and missing-evidence suspension. It
compares the snapshot again and atomically commits all changes, field views,
runtime assignments and the completed job. Retrieval does not increment the
permission revision or release requester access.

Attempt manifests record loaded source digests, loaded ledger IDs, context
counts, a full-prefix digest and the existing snapshot/prompt hashes. They do not
copy raw retrieved source text. The full source-ID prefix remains host-side job
lineage; this change reduces model input, not the host's full-prefix loading and
validation cost. Context size can still grow with relevant authorization
evidence, unresolved work, the compact ledger index and global restrictions.
Semantic routing and necessity judgments remain model-dependent; a missing
search result never proves that permission is absent.

This changes the prompt to `permission-inference-v9-incremental`. Start a fresh
benchmark directory with `--permission-version 9`; old official results and
unfinished jobs under older prompts must not be relabeled or resumed as v9.
The [offline validation and reproducible request-size check](analysis/incremental-permissions-20261029/VALIDATION.md)
record the security coverage and synthetic input reduction; real-model quality
and latency have not been measured for this change.

## Bootstrap and authenticated ingestion

Construct `PermissionBootstrap` with:

- `participants`: authenticated IDs and contextual attributes, including roles.
- `resources`: known subject/resource IDs, with domain-specific descriptions.
- `source_identities`: authenticated connector IDs and the participant IDs each
  connector may attest. This is a trusted authentication adapter configuration.
- `authorities`: per-resource `permission`, `restrict` and `delegate` capabilities.
- `task_convention` and `task_actions`: the task-based access convention. The
  default actions are `search`, `read`, `derive`; editing, forwarding, exporting
  and delegation require separate justification and authority.
- Optional `initial_permissions`, `context`, `timezone` and
  `workflow_identities`. An initial permission requires an ID, participant,
  subject, actions, information scope, source receipt and original scope text.
- Optional `discover_self_owned_participants`: authenticated connector IDs that
  may introduce new speakers through actual source events. This permits an empty
  initial roster. Only observed speakers become participants and self-controlled
  information resources; it supplies no read grants or authority over others.
  The default GateMem protocol uses this option instead of fixture metadata.

Roles and relationship descriptions are context, never permission receipts.
The engine has no patient concept or domain-specific subject fallback.
Initial permissions remain scoped templates: disclosure of source text still
requires an exact binding and its necessity/scope assessment.

The trusted host issues an ingestion session with `manage_policy`, `ingest`,
`read`, `derive`, `search` and the desired lifecycle/audit capabilities. Register
the permission processor and approve its processing route in the session before
constructing the updater. The updater installs host-only source and field
policies. It never gives the processor session-issuing or policy-installing tools.

```python
from agentic_memory.privacy import (
    PermissionBootstrap, PermissionSourceEvent, PermissionUpdater,
)

bootstrap = PermissionBootstrap(
    participants={"owner": {"role": "project owner"}, "worker": {}},
    resources={"project": {"description": "A controlled project"}},
    source_identities={"project-chat": ["owner", "worker"]},
    authorities={"project": {
        "permission": ["owner"], "restrict": ["owner"], "delegate": ["owner"],
    }},
    task_convention="Accepted project work permits only information needed for that work.",
)
updater = PermissionUpdater(core, host_context, compartment, bootstrap, processor)
job = updater.ingest(PermissionSourceEvent(
    id="message-1", participant="owner", source_identity="project-chat",
    text="Worker, please prepare the release notes for this project.",
    subject="project", occurred_at="2026-10-26T09:00:00+08:00",
))
updater.update(job["job_id"])
```

`PermissionSourceEvent` is a text source-event interface. The host authenticates
upstream attribution before calling it. Source IDs are immutable; the same event
ID and contents reuse their job, while changed contents are rejected. Source
storage and the pending job commit together. Questions are permission evidence;
their guessed factual values do not become indexed facts merely by being asked.

## Structured decisions and publication

`proposal_schema()` and `CONTRACT_NOTES` describe the closed output contract.
The v6 prompt explicitly separates positive permissions from negative restrictions,
recognizes a subject's acceptance of a worker's particular task as corroboration,
and requests bindings for new facts needed by existing assignments. Source-grounded
requests or acceptance of one's own task may support a narrow self-assignment;
identity ownership alone still grants nothing. The GateMem bootstrap states this
self-use convention explicitly and enables `self_use_actions` (search/read/derive).
After a validated established activity contains authenticated subject acceptance,
the compiler creates an empty, activity-bounded self-use assignment. It copies no
worker fields: every subject field still requires its own original-scope evidence,
necessity assessment and restriction checks. Completion or missing evidence closes
this access; a source identity by itself creates no assignment.
The convention is: a subject requesting or accepting help authorizes
their own access to responsive information for that activity. This convention
requires an authenticated source statement and does not extend to other actors. Network proposals must choose either an enforceable active assignment or
observation/no grant; they cannot indefinitely bind information to an unusable
proposed assignment. The canonical host API still permits intentional staging.
Response schemas generate `op` and
`basis` before basis-specific fields, so an explicit grant cannot be forced into
the task branch by an unrelated activity ID. Restrictions require `effect: deny`
and self-checks for the actual prohibition and exclusion of the permitted subset.
Final responses contain `revision`, `considered_restrictions`, and `operations`,
plus `coverage` when requested. Optional `context_requests` requests missing
context before a final proposal, as described above.
`revision` must echo the current input integer exactly; only the host increments
it after a successful publication. The per-call response schema pins that value
and limits identities, source references, activity bases and statuses to the
current contract/prefix. Network schemas also pair each resource with its actual configured authority
and authenticated authority/corroboration sources, reducing repeated impossible
proposals before host validation. Host validation still rejects stale or malformed output
and leaves the job pending. Repair feedback reports the expected and received
revision; the host never silently rewrites a model's revision.
Operations are:

```
observe_participation  establish_activity  propose_assignment  bind_information
record_restriction    schedule_expiry     suspend_assignment  revoke_assignment
end_activity
```

Each substantive conclusion has its own `evidence` entry, referencing available
source IDs. Information needs and unresolved questions carry individual evidence
references too. Previous interpretations are included as state, with their
underlying source evidence; interpretation IDs cannot serve as corroboration.

Involvement statuses are `observed`, `tentative`, `established`, `ended`.
Assignment statuses are independently `proposed`, `active`, `suspended`,
`expired`, `revoked`. Proposed assignments may become active with a new validated
proposal; revoked/suspended assignments require a new assignment ID and fresh
justification. An independent explicit permission does not require an activity.

The evidence criteria are:

| Basis | Treatment |
| --- | --- |
| Authorized explicit permission or trusted initial receipt | Stated scope, with exact field bindings |
| Corroborated task performance | Justified task-specific bindings |
| Authorized handoff | May establish involvement before performance |
| Performance with unclear relationship or corroboration | Tentative involvement, no task grant |
| Presence, questions, offers, self-asserted authority | Observation alone, no grant |

The schema requires structured semantic self-checks for establishment,
assignment basis/actions/scope, and each information binding. The latter explains
necessity, the original authorized scope, preserved context, safe metadata and
restriction handling. Assignments retain `scope_evidence` containing the verbatim
original scope statements. Every binding must cite that original evidence through
`evidence.authorization`; a new fact alone cannot authorize its own disclosure.
A broad category name or absence of a prohibition never widens the permitted scope. Each binding
also records an evidence-backed applicability decision for every current
restriction, including restrictions whose semantic scope uses different labels.

Bindings select ordered, nonoverlapping character spans from one exact source.
Network processors use a compact wire format: stable short source aliases replace
hashed source references, and `quote` selects a unique exact substring. The host
expands aliases and resolves the quote into character spans before validation.
Ambiguous, inexact, or fabricated quotes are rejected; there is no fuzzy matching.
Canonical IDs and resolved spans remain in the persisted audit ledger. Scripted
processors retain the canonical span interface for deterministic tests.
Code constructs the text from those spans; the model cannot supply replacement
text or requester-visible labels. Only selected assertions or instructions can
be indexed as information. Exact selectors prevent fabricated excerpts, but
semantic appropriateness, corroboration and context preservation still depend
on the model's interpretation.

Accepted bindings compile through `install_assignment(resource_ids=...,
evidence_relationships=...)`. These assignments never bind new records merely
because their category matches. Later records require a new necessity decision.
Permission evidence dependencies are separate from document-read dependencies:
a recipient can read an approved field without receiving its establishing
conversation. Inferred field views expose selected text and opaque IDs; control
metadata and source selectors remain private.

Restrictions are independent, append-only versioned records. They identify the
authorized issuer, affected identities/resource/information/actions, source
evidence, effective conditions and any superseded restriction. The runtime
checks them across old and future assignments. Supersession needs a later
authenticated issuer statement. A scheduled replacement cannot lift an earlier
restriction prematurely. Lost reauthorization evidence conservatively restores
the earlier restriction pending reassessment. Restrictive records themselves
survive source deletion until an authorized replacement changes them.

## Time, jobs, corrections and recovery

Sources record `observed_at` separately from `occurred_at`. Validity supports
`effective_from`, `effective_until` and `end_activity`. Relative calendar time
requires a source timestamp/timezone; ambiguous time remains unresolved. Inferred
duration defaults to its activity, with explicit `end_activity` completion and
no universal timeout. Runtime conditions enforce deadlines even without another
model call. Completion expires task assignments while preserving independent
permissions unless their own end condition names that activity.

Each source event has a job with input IDs, permission revision, state snapshot,
model and prompt version, proposal, validation outcome, resulting changes and
attempt count. Attempts retain input digests and prompt hashes, rather than
copying source text into an additional audit archive. Calls run outside database
transactions. Publication atomically verifies the permission snapshot and
commits the ledger, field views, assignments and completed job together.

While any job is pending, failed, rejected or conflicted, requester access to the
entire enclosing compartment is blocked, including buffered release. Only its
trusted policy host can process it. Later source ingestion cannot advance past
an incomplete event. `update()` performs up to three contract-repair attempts
with validator feedback; `run()` performs one attempt. Transport failures remain
resumable. Resume via `pending_jobs()` and `update(job_id)`.

The GateMem runner adds supervised retries around these bounded repair rounds.
It waits at the same source on known model failures, records recovery status and
never advances past unresolved permission work. Pending jobs are restored at
their own observed timestamps before replaying earlier turns. Recipient coverage
still requires every listed principal and live evidence; its diagnostic change
label is derived by the host from validated recipient operations. The attempt
audit retains both label adjustments and the original model coverage. These
adjustments cannot authorize an invalid grant or binding.

Use `correct(new_event, replaces=source_id)` for authenticated upstream correction
metadata. It atomically stores the replacement, records source supersession and
queues permission reassessment. `core.delete()` and `core.supersede()` also queue
reassessment through permission-specific evidence dependencies. Missing evidence
makes involvement uncertain and suspends dependent assignments. Original
completed job results remain available for replay; reassessment uses a separate
job. Natural-language instructions cannot directly execute source deletion.

Changing model or bootstrap convention requires an explicit new updater
`version`. Bootstrap versions and attempt histories are retained. A new version
can replace unfinished jobs; the old jobs record their replacement. Changing the
bootstrap also blocks access until reassessment, and existing grants under the
old convention require fresh justification. A stale updater cannot publish
under a newer configuration. Fixed GateMem runs additionally reject changed
code/model/configuration manifests on plain resume. An explicitly acknowledged
code-only repair continuation records implementation revisions, preserves the
original manifest and predictions, and labels the result as mixed implementation;
it cannot bypass model, dataset or causal configuration checks.
In-place reprocessing under a new version targets the current source prefix.
Historical replay under another model uses a fresh compartment/store, so later
permission state cannot enter an earlier event's inference input.

## GateMem comparison and verification

The runner defaults to `--permission-mode inference`, using
`evaluation/gatemem_medical/inference_adapter.py`. The evaluator retains the
original episodes, `entities`, checkpoints and scoring labels as ground truth.
`input_boundary.py` gives the adapter only an episode routing ID and each new
source delta. A turn contains only `text`, `speaker.principal_id`, `turn_id`, and
optional `timestamp` / `turn_kind`. Text is preserved verbatim. Structured roles,
relationships, access levels, subject hints, source context and arbitrary extra
annotations are dropped before ingestion. In-dialogue consent and event notes
remain valid evidence when their turns arrive.

There is no advance roster, subject map, controller grant or initial permission
receipt. The fixed bootstrap starts with empty participants, resources and
permissions. Its opt-in `discover_self_owned_participants` connector convention
resolves identity envelopes from the live source prefix for each job. Each observed
speaker can authorize/restrict only its own information resource; that fixed
self-control convention grants no readable memory. The model must ground subject
association, relationships, accepted tasks, grants, restrictions and exact
information bindings in source text. It may leave them unresolved. Speaker ID
spelling supplies no access entitlement. An unseen recipient cannot receive a
grant until its identity appears in the source stream.

The configured source connector authenticates speaker IDs. The evaluator separately
authenticates `asker.principal_id` for queries, including previously unseen askers;
queries never add source identities, alter permission state or grant access.
The host compiles validated inferred assignments into enforced policies and
directed edges. Answer generation receives only authorized memory evidence.
Naive timestamps use the fixed UTC simulation convention. Permission processing
runs once per source event regardless of checkpoint placement. Checkpoint labels,
expected answers, judge feedback and future turns never enter ingestion input.
The run manifest and summary identify this contract as `raw-stream-v1`.

Resume reconstructs the same prefix from persisted observations without resetting
permissions or consulting `entities`. Use a **new run directory** for this protocol;
old metadata-assisted results are not comparable, and manifest checks reject mixing
them with a raw-stream run.

The original medical compiler and adapter remain available as
`--permission-mode baseline`, explicitly labeled `metadata-assisted-oracle-baseline`
in manifests and summaries. It reads fixture relationships and is an oracle
comparison, not a valid raw-stream benchmark result. Use separate run directories:

```bash
python3 -m evaluation.gatemem_medical --permission-mode inference --run-dir runs/inference
python3 -m evaluation.gatemem_medical --permission-mode baseline --run-dir runs/baseline
```

The summary includes permission job/attempt/version counts alongside the existing
utility and privacy metrics. `--no-evolution` disables ordinary evolution without
disabling permission inference. `--offline` uses an explicit empty permission
double for execution/replay smoke tests; its scores do not measure inference
quality. The updater supplies the incremental context described above. The compact
network format omits redundant diagnostic prose from loaded ledger records and
aliases loaded source references. Selected source text, attribution, authority,
evidence, restrictions and validity remain present. Network JSON omits unnecessary
whitespace. Republishing an immutable field reuses
its embedding in the pinned space, and only newly created fields are sent to
ordinary evolution; runtime assignments still retain every authorized target.
Evolution uses short note aliases inside the trusted network serializer, restored
before the core issues its canonical processing ticket. Metadata updates target
only newly created notes; older notes remain available as graph link endpoints.
The full canonical input lineage and publication checks are retained. The model server
explicitly enables prefix caching (opt-in for its hybrid architecture); changing
this flag requires restarting the service. Source events remain sequential and
independent of checkpoint placement; no checkpoint-driven permission batching or
parallel publication is used.

Permission job validations record model elapsed time and token usage; summaries
include active assignments, indexed resources, model seconds, and average time
per source. Attempt totals include rejected/repair attempts, not just accepted
calls; per-source log usage is captured before the separate evolution call. The runner logs source progress and completed-boundary wall time.
Use a fresh run directory after this implementation/prompt change; old completed
jobs and official predictions must not be reused as repaired results.

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
```

Tests cover the deterministic authority boundary and scripted semantic decisions.
They do not establish real-model inference accuracy. Structured self-checks are
fallible; a separate semantic review call is not enabled. Its utility/privacy
benefit and model cost should be measured against the retained baseline before
adopting it.
