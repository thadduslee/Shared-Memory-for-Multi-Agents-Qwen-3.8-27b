# Implemented two-store privacy memory

This document describes executable behavior. `PRIVACY_ARCHITECTURE_PROPOSAL.md`
is historical design material; it does not describe the old experiment's storage
layout. The guarded API is `agentic_memory.privacy.PrivacyMemorySystem`.
The legacy `AgenticMemorySystem` API remains unguarded.

## Storage and schema

The runtime uses two **different persistent SQLite files**, each with vector
rows. Exact vector ranking is appropriate for the current local dataset and
avoids introducing a server, separate credentials or an eventually consistent
vector service. Vectors are normalized JSON arrays; there is no approximate
index whose unrestricted top-k can displace authorized evidence.

| Database | Durable tables |
| --- | --- |
| `memory.sqlite` | `resources`, `representations`, `provenance`, `source_views`, `revisions`, `tombstones`, `events`, `metadata` |
| `memory.privacy.sqlite` | `policies`, `memberships`, `authority_owners`, `assignments`, `access_edges`, `privacy_vectors`, `privacy_metadata`, `assignment_diagnostics` |

The medical adapter also owns `privacy.erasure_selectors`, containing hashes,
selector lengths and source event IDs, not plaintext deleted contact values.
Canonical SQL definitions are in `agentic_memory/privacy/storage.py`.
`PrivacyMemorySystem(path, embedder, privacy_path=...)` accepts an optional
explicit second path; the default is `path.with_suffix('.privacy.sqlite')`.

Resources have immutable stable IDs and payloads, an explicit policy reference,
compartment, authoritative attributes, dependency IDs, captured derivation
ceilings, state and creation time. Access edges in the privacy file point to
those stable IDs. Each edge selects a policy and optionally an assignment;
policies carry actions, subjects and conditions. These bindings are evaluated
at every use, not permanent read guarantees.

Semantic edges are protected memory resources with `from`, `to` and predicate.
They never add authority. Derivation dependencies constrain every contributing
input, including uncited model inputs and graph edges. `source_views` is a
separate lifecycle relation used only by authority-approved field projections;
it does not pretend whole-document permission and field permission are equal.

## Consistency, publication and recovery

Both SQLite files are ATTACHed to one connection. They must be ordinary files
on one local filesystem. The implementation sets **DELETE rollback journaling**
and FULL synchronization on both, so SQLite's multi-database super-journal
coordinates commit. This deliberately avoids WAL, which does not provide this
multi-file atomic commit property. An in-process reentrant lock and
`BEGIN IMMEDIATE` serialize publication/revocation/release; other connections
participate through SQLite locking. Filesystem/storage durability remains a
host responsibility; network filesystems and distributed writers are not a
demonstrated deployment target.

`create` writes a staged resource, its primary access binding and every matching
assignment binding inside one transaction, then activates it. Source ingestion,
field creation and embedding occur under their enclosing transaction. A thrown
failure before activation rolls back both databases. The tests also terminate
a subprocess during a paired write and verify rollback on restart. They do not
simulate loss of power or arbitrary disk corruption.

Cross-file references cannot use SQLite foreign keys, so publication checks
policy ownership, exact referenced IDs, dependencies and tombstones in code.
Memory-local foreign keys protect representation/source-view relations. A
resource without a current policy/binding is unavailable; an absent or deleted
field source fails closed. Dangling bindings cannot grant access to an absent
resource. A random `store_pair` identifier in both files prevents opening a
missing or mismatched half as a valid pair. Startup raises an operator-visible
error rather than creating replacement authority for an existing memory store.

Stop writers before backups and copy/restore **both files as one consistent
snapshot**. Restore the pair and replay committed source events with stable IDs
if necessary. Do not independently replace one file, use WAL on either file,
copy a live pair piecemeal, or manufacture policies to repair a missing half.
Boundary event digests detect conflicting replay; tombstones prevent source ID
resurrection. No model output can repair or install authority.

## Deterministic query flow

1. The trusted host authenticates an identity and issues an opaque scoped
   session with actions, compartments, purpose, expiry, processing routes and
   optional attenuating delegations. Caller text cannot issue a session.
2. Exact memberships and access-edge joins select applicable policies and
   assignments. Every applicable explicit denial wins. Processor approval,
   exact subject/information scope, purpose, validity and live delegation parents
   are mandatory. Revoked assignments cannot grant through a still-present edge.
3. The engine enumerates permitted active memories before computing vector
   scores. Search checks search, read and approved embedding/derive rights for
   each candidate. It does not filter only an unrestricted nearest-neighbor list.
4. Ranking combines vector similarity and lexical overlap over source text and
   trusted adapter retrieval terms, excludes question stopwords, and reserves evidence
   for bounded query facets, favors recent source sequence for current-state
   queries, and removes duplicate base/enrichment/text results. Explicit revision
   chains and their descendants do not rank as current; history stays addressable.
5. Graph expansion checks each edge, target and dependency independently. The
   complete processing input manifest includes consumed edges and all evidence.
6. Before a model call the engine verifies derive and approved-processor rights
   for every input. It rechecks after the call. Buffered output release checks
   all inputs again. Exports require both sender export and recipient read rights.

Policy vector search is an issuer-only discovery helper. Authorization never
calls it; missing/stale privacy vectors cannot hide a denial or invent a grant.
The core does not infer role hierarchies or treat author/owner metadata as read
permission.

## Generic assignments and authoritative field views

`install_assignment` accepts exact principal/group, subject, compartment, task,
information scope, actions, optional relationship/collection, validity/purpose,
parent delegation, issuing authority and supporting provenance. Its condition
grammar is closed; unknown conditions fail validation. Only the owner of the
underlying policy can publish bindings to its resources. Delegations are live
attenuating parent references, not permanent copied grants; revocation and
expiry propagate. Groups are resolved exactly from current memberships.

`ingest_authoritative_view` requires the source policy authority. It selects
exact character spans before any model processing and records their source
locator. Optional trusted field labels preserve time/status/location grouping
without releasing disallowed surrounding specialty labels. The authority explicitly approves a field policy for that view. The
view retains source deletion/lifecycle dependence without requiring its reader
to read an unrelated whole document. Arbitrary readers and model-generated
“sanitization” cannot use this API to broaden sharing.

Ordinary generated artifacts always retain every input dependency. Their
captured ceilings preserve the source audience at derivation time, evaluated
against the source's captured ID/kind/attributes. Live source checks also apply;
adding a new direct allow rule alone does not widen an old artifact, and
revocation immediately blocks it. Captured rules still resolve current group
memberships; this is a rule ceiling, not a frozen list of historical recipients. Mixed-scope enrichment stays separate from immutable base
notes. This is tested using shipping fields and private margin data in a
nonmedical warehouse example as well as the medical fixture.

`supersede` records an explicit revision event after checking write rights and
same compartment/subject. It never guesses that every later sentence replaces
all prior facts. The medical adapter records source sequence and preserves
cancellation/current-state wording. Determining complete semantic supersession
from arbitrary prose remains an adapter limitation.

## Embeddings, persistence and caches

Qwen reads/generates text; MiniLM embeds text. No representation alignment between
those models is needed. Every index uses its embedder's space fingerprint.
`LocalEmbedder` resolves a local cached snapshot and fingerprints actual weights,
tokenizer/config files, revision, dimensions, normalization, sequence length and
relevant library versions. A same-dimensional different model gets a different
space ID. Legacy model-name-only vectors cannot silently enter the new space.

- **Persistent memory:** authoritative source/resource rows, retained independently
  of vector availability.
- **Vector indexes:** disposable representations in each file, keyed by resource
  or policy ID and embedding space. They are not permission caches.
- **Authorization caches:** none. Membership, rule, validity, delegation and
  dependency checks use current authoritative state on each use.
- **Processing tickets:** short-lived in-memory full-input manifests. Policy,
  membership, assignment revocation, revision and deletion clear them. Application
  rechecks also protect expiry and concurrent revocation.
- **Saved evaluation predictions:** immutable experiment artifacts pinned by code,
  data, embedding and causal-run fingerprints. They are never reusable live
  authorization results or source memories.
- **Model caches:** no application answer cache or weight updates are introduced.
  External serving/KV caches are outside the demonstrated storage/deletion scope.

Policy changes re-embed their policy row atomically. Membership/revocation/expiry
need no memory re-embedding because authorization is live. Source revisions use
new immutable IDs and explicit revision links. Deletion tombstones the source
and all ordinary/view descendants, clears searchable vectors and blocks replay.
`rebuild_index` only embeds active approved resources and issuer-owned policies;
it skips deleted/quarantined/non-evidentiary records. Changing the space requires
an explicit rebuild, even when vector dimensions do not change.

## Migration and rebuild

Preserve old run directories. The new core refuses to open an old combined file
in place. With writers stopped, migrate a **copy** to a new output directory:

```bash
python3 -m agentic_memory.privacy.migrate OLD/memory.sqlite NEW/memory.sqlite
```

The command opens the old file read-only, copies canonical rows and tombstones,
and creates exact primary access bindings in the new privacy store. It refuses
existing output paths. Vectors are deliberately omitted. Open the new pair with
the approved embedder, issue an authenticated service session for the original
policy issuer (with ingest/manage_policy/derive/read and approved embedding
route), then call:

```python
core.rebuild_index(service_context, authorized_compartments)
```

Repeat for each independent issuer/compartment; migration does not claim another
issuer's authority. Check policy/resource counts and intended denials before
serving. Migration preserves legacy IDs as well as old adapter events and permissions; it does not
retrofit new task interpretations into old checkpoints. New adapter source IDs, policy references and compartments hash the episode key
so requester-visible provenance does not disclose diagnoses embedded in fixture
filenames. To measure the new medical adapter with those opaque IDs, replay raw source fixtures into a **fresh** run directory.

```bash
python3 -m unittest discover -s security_tests -v
python3 -m unittest discover -s evaluation/gatemem_medical/tests -v
python3 -m evaluation.gatemem_medical.access_audit \
  --output runs/NEW-EMPTY-local-audit
```

`access_audit` makes zero model/network calls. It writes per-query access and
retrieval dispositions for the 118-case cohort, plus all 210 utility, 192 privacy
and 177 safety retrieval cases. An annotation-selected cohort and retrospective
scope labels are reporting inputs only, applied after the live adapter has
constructed authorization and retrieved evidence. Auxiliary literal checks are
not an independent privacy evaluation or model-quality measurement.

For actual generation/judging use a new directory with the existing runner only
after authorizing its compute/network costs. The local functional `--offline`
runner supports the same journal/resume path but produces no real-model score.

## Medical interpretation and remaining limits

See `evaluation/gatemem_medical/AUTHORITY_POLICY.md` for the explicit medical
workflow convention, source authority provenance, exclusions and unresolved
cases. This is not a recovered universal hospital staff matrix. Unrecognized
prose, fields, assignment windows or clinical preparation can remain unavailable.

Diagnostics are operator-only: absent assignment, explicit denial, expired or
revoked scope, blocked dependency and missing evidence are distinct from public
unavailability. The local audit separately records scope retrieval gaps; a real
model refusal or incomplete answer requires actual generation/judging and is
not inferred from the existence of evidence.

The suite tests logical deletion and removal from the active vector index. It
does **not** claim physical erasure from SQLite pages, rollback journals/WAL from
old runs, backups, external fixtures, model providers, evaluation artifacts or
previously delivered outputs. Filesystem confidentiality, production identity
providers, encryption, distributed fencing and arbitrary natural-language policy
interpretation remain deployment work.
