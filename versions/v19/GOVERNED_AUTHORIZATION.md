# Governed authorization, version 7

The source adapter attests identities and source envelopes. The authorization
learner proposes tasks, assignments, exact information bindings, restrictions,
and lifecycle actions. The trusted updater validates these against its configured
authority contract. The governor records generic capability candidates and host
decisions before installing runtime grants. The core enforces those grants on
search, read, processing, graph traversal, and release.

## Reusable interfaces

- `PermissionSourceEvent` is the authenticated evidence envelope. Its participant
  and connector are supplied by the host, not extracted from a claimed role in
  query text. Bootstrap configuration determines which connectors and issuers
  are trusted and which task conventions may justify access.
- `AuthorizationLearner.propose()` routes model inference through core processing
  checks. It cannot activate a permission. `feedback(namespace)` exposes only
  the issuing host's protected decision history for that namespace.
- `CapabilityCandidate` contains a principal, namespace/attribute/resource-ID
  selector, actions, intended purpose, evidence IDs, issuer, and validity.
- `PolicyGovernor.activate()` requires a host policy-management session, live
  same-namespace evidence, an authority receipt, and targets that match the exact
  selector. Empty targets are recorded as approved-but-unbound and grant no
  access. `proposed`, approved, revoked, suspended, and expired decisions are
  retained separately from runtime assignment state.
- Existing `install_assignment()` callers remain supported. The generic path
  accepts a `resource_selector` with opaque attributes; neither the governor nor
  core requires medical classes. Intended purpose is audit context; a purpose
  restriction is enforced when supplied in assignment conditions against the
  host-issued session purpose.

The model's confidence or an `authenticated: true` assertion is not an approval.
The authenticated connector establishes authorship, while configured issuer
authority and source-grounded consent/workflow evidence establish permission.
Semantic interpretation and task necessity remain fallible model judgments;
structured self-checks do not prove them correct. Human/service approval can be
implemented at the host decision boundary without weakening kernel checks.

The optional `read_pipeline_actions` adapter convention expands a source's
explicit read permission to search and approved processing of the SAME fields
through this assistant. GateMem enables it, because literal read-only grants
otherwise cannot enter its search/read/derive answer pipeline. It adds no
write/export/forward/delegate permissions and cannot override a denial. The
generic kernel and adapters without this convention retain distinct actions.

The current learner performs inference and records authority feedback. It does
not automatically train itself or promote repeated patterns to authority. A
future learned template still needs an approved authority envelope. Domain
adapters and explicit authority/policy profiles may vary; core enforcement stays
the same. Gold benchmark answers and future turns remain evaluator-only.

## Positive access and recipient coverage

Real permission calls must account for recent participants, pending activities,
and active recipients in `coverage`. Each decision cites source evidence and
states changed, unresolved, or unchanged. The host rejects omitted principals
and claimed changes without operations, and stores decisions in
`permission_coverage`. This makes omission visible but does not prove semantic
coverage: a model can still incorrectly say unchanged. Real-model validation is
required alongside deterministic security tests.

`binding_authorizations` explicitly lists original scope evidence for each
existing assignment. Repair feedback identifies the assignment and required
source references. New facts alone cannot authorize themselves. A newly accepted
task can bind earlier necessary facts from its available prefix; a fresh narrow
grant is required for a scope expansion. Medical scopes stay in the medical
adapter/fixture interpretation, never a core occupation-to-permission table.

## Erasure

The learner can propose `erase_information` with an authoritative issuer, subject,
exact source-derived value selectors, current execution evidence, and per-claim
evidence. Future effective times are rejected. A conditional request must not be
executed until source evidence supports execution; this semantic interpretation
remains subject to model error. Owner authorship or a previous subject-bound
field is required for each selector, preventing selectors from unrelated
subjects. Authority is checked independently of the worker reporting completion.

The host stores normalized selector hashes in `source_erasures`, removes matching
readable fields and dependent notes/enrichments/edges/index entries, prunes their
bindings, and suppresses later matching bindings. Release checks also enforce
the erasure rule even if a new grant references a repeated value. Numeric
identifier punctuation is normalized. Unaffected fields and other subjects
remain available. Selector storage, erasure cascades, ledger changes, and
approval decisions commit or roll back together.

This is logical erasure from requester-accessible memory, not physical erasure
of fixtures, host-only source audit observations, permission audit history,
SQLite pages/backups, or already-delivered output. Erasure rules are persistent
within the subject/namespace. Arbitrary semantic paraphrase detection and a
workflow for reinstating an erased value are not implemented.

## Runtime cost and retrieval

Version 9 adds [incremental permission context](PERMISSION_INFERENCE.md#incremental-context-version-9):
recent dialogue, relevant control records and original authorization evidence,
bounded binding samples, all denials and a compact ledger index. Missing evidence
can be retrieved before proposing changes. Full canonical validation and atomic
publication remain host-side; raw conversation replay is no longer the default
model input. Use a fresh version 9 run to evaluate this prompt change.

Permission state loads only current ledger versions. Read results are reused
inside a locked SQLite transaction, invalidated on any same-connection SQL
mutation and discarded at commit/rollback. Session expiry/revocation is checked
even for cached authorization; clock-dependent decisions include the trusted
clock value. A later model call or release opens a fresh snapshot. Applicable
denies are retained when irrelevant assignments are filtered before liveness
checks.

Inference views carry source sequence values. Current-plan retrieval reserves a
small portion of its authorized budget for recent instructions, alongside
multi-part relevance ranking. Source projection prompts require complete
conditions and cancellations. Recency cannot grant access or establish factual
supersession by itself.

`pipeline_timings` measures whole-source ingestion, permission processing, and
whole evolution including neighbor search. Summaries distinguish permission
processing seconds from observed model-call seconds. Counts expose capability
decisions, recipient decisions, and erasure selectors.

## Answers and evaluation

The answer model receives an assignment-availability status and already
authorized evidence. Generated scope labels, authority receipts, and policy
metadata remain private because their wording can itself contain sensitive
context. Empty access without a supported assignment returns refuse;
missing evidence under an existing assignment returns no_memory. The prompt
distinguishes answer, answer_redacted, refuse, and no_memory, including erasure
recovery. These labels do not reveal hidden values.

Real answer calls first classify requested parts using authorized evidence.
Supported parts need provided evidence IDs. Unsupported output is discarded when
no requested part is supported; the application chooses the final response
class instead of accepting an inconsistent model label. For an ambiguous
missing fact, a separate read-only host classifier can inspect the requester's
approved scope labels and return only per-part outside-scope booleans. It never
receives hidden record contents, exposes no scope labels to the answer model,
and cannot create permissions. Scope uncertainty remains missing. This extra
classification runs only for parts marked missing, not every answer.

For utility, the independent judge must return a verdict for every required
include item; one uncovered item overrides an optimistic overall pass. When a
context regex flags possible disclosure, the real judge reviews context
semantically, preserving the original auxiliary flag for audit. This reduces
word-matching false positives but does not make model judging infallible.

## Validation and fresh runs

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
python3 analysis/fixes-v7-20261028/validate_snapshot.py
```

The last command opens a disposable copy of v6 and compares cached/uncached
decisions. Stored vectors isolate authorization/search cost; its timing is not a
whole-model benchmark. The separate `semantic_smoke.py` runs real models on a
nonmedical release-coordination stream, including task acceptance, narrow
logistics, contact instructions, cancellation, and erasure. Its diagnostics are
not official GateMem results. Resume skips old probes, preserving their original
source prefix.

Use a fresh official run directory; v6 predictions and databases are preserved:

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 7 \
  --run-dir runs/gatemem-medical-raw-stream-v7
```

Completed old runs cannot be resumed under changed code/prompt manifests. A
focused successful diagnostic is not a claim of full-benchmark accuracy.
