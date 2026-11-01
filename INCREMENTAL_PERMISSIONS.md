# Incremental permission processing (v15)

The raw-stream runner now defaults to version 16; see
[`analysis/over-refusal-v16/README.md`](analysis/over-refusal-v16/README.md) for its
grounded worker receipts, original-authorization binding context and verification.
For the reusable API select `PermissionUpdater(..., version='16')` explicitly;
the API's version-15 default remains for compatibility. The rest of this document
records the historical version-15 contract.
Use a fresh store/run. Versions 13 and 14 remain explicitly selectable; historical
stores, skipped turns, official predictions and scores are not migrated or rewritten.

## Source processing

1. An authenticated connector stores the event under a host-only source policy.
   A pending control job prevents requester access while its interpretation is unresolved.
2. One model call receives six recent events, concise current access state, persisted
   tasks, exact current segments, a bounded fact index, protected-history candidates
   and pending recipient pairs. Speaker/resource, record, source and candidate
   references use opaque aliases.
3. In that response, the model independently reviews every current segment for
   control changes, classifies factual segments, proposes grants/restrictions/task
   changes and assesses recipient bindings. Facts and controls are different decisions.
4. Exact source segments become durable protected candidates even with no grant.
   The host derives IDs, source spans and binding status. It does not infer semantic
   authority, scope fit, consent, subject attribution or replacement from keywords.
5. The canonical validator checks the complete control transition. Binding failures
   remain unavailable individually; they do not impose a compartment control hold.
   Accepted control changes and accepted readable fields publish atomically across
   the existing two SQLite stores, after a source/policy snapshot check.
6. At most one additional semantic call repairs failed decisions or assesses historical
   facts for a newly established grant. Successful independent decisions survive the
   repair and process restart. Transport outages do not reset or consume the durable
   semantic-rejection budget. Interrupted publication reuses accepted work without
   another model call. Optional semantic enrichment remains outside source processing.

`control_review` is a source-grounded model assessment, not a proof of complete
natural-language understanding. After the durable two-call budget is exhausted,
`update()` raises. A streaming application can explicitly call
`skip_exhausted(job_id, max_attempts=2)`; the v15 adapter does so automatically.
The rejected delta is never published. The job becomes `quarantined`, and
`permission_control_holds` records the subjects that the authenticated sender can
control, plus subjects of their worker/requester tasks. This uses authenticated
capabilities and saved task relationships, not text keywords or claimed roles.
An owner's unresolved revocation still blocks that owner's data, including future
grants. A stranger's failed claim cannot blanket-block an unrelated subject.
Unattributed resources stay conservative; trusted workflow identities with
unbounded control retain a compartment-wide hold. Automatic resolution of those
unbounded holds is outside this prototype.

A later authenticated control authority statement can explicitly
`resolve_control_hold` after the model assesses the failed source and current
clarification together. Historical failed text is not fresh consent. Resolution
alone neither grants access nor removes a committed denial. Deleted or corrected
resolution evidence reinstates the hold. Queries cannot resolve holds. Legacy
v13/v14 skipped jobs keep their existing broad holds; they are not migrated.

## Empty access and generality

The raw adapter starts with `participants`, `resources`, `authorities` and
`initial_permissions` empty. Its authenticated connector discovers speakers only
as events arrive. No role, relationship, access-level, subject mapping, episode
name, future event or evaluation annotation supplies a requester grant. The model
receives opaque identifiers; source text remains verbatim.

The fixed authority framework still defines what ownership/control and accepted
work mean. Discovery establishes who may control their own information; it grants
no read rights. V15 rejects nonempty `initial_permissions` and automatic
`self_use_actions`: even the subject's own read assignment requires an explicit,
source-grounded model proposal. Host ingestion/control policies exist so the service
can store and interpret protected data; those are not requester access grants.

The engine has no medical role/field compiler and imports no benchmark modules.
The same APIs serve project coordination, parcel collection, personal scheduling
and the medical proof of concept. Connectors must authenticate event speakers and
application callers; the prototype does not implement an external identity provider.

## Incremental work and latency limits

New facts are assessed under existing active assignments. New grants can bind retained
facts. Exact accepted pair decisions are cached against candidate, assignment,
task, relevant restrictions and authority evidence. Changes invalidate affected
reviews. Unrelated recipients' restrictions do not invalidate a valid decision.
No applicable assignment is a protected fact state, not a reason to repeatedly
ask for new authorization. Factual history cannot supply fresh consent/revocation.

Each response reviews at most 12 candidate/assignment pairs. New arrivals receive
priority, then the oldest pending work. Up to four incomplete historical extraction
segments can join the next turn's existing call. Overflow is durable and unavailable,
never silently allowed or discarded. The relationship index routes at most 32
retained facts (recent facts plus older lexical matches); unselected facts remain
in storage. This routing is not a semantic recall guarantee and never changes
access. A repair receives only pending-pair facts and the preserved prospective
grant shortlist. Control/fact response arrays are bounded by requested segments.
`processing_metrics()` exposes pending reviews
and incomplete extraction; affected query packets set `context_complete: false`.

For a large late-grant backlog, the application can explicitly call:

```python
updater.flush_bindings(max_batches=2)
```

Each maintenance batch makes one semantic call over at most 12 pairs. It cannot
create grants, change controls or run while controls are held. It checks the same
snapshot, canonical binding validator, source liveness and release rules. It stops
on no progress; rejected maintenance has a durable two-attempt budget. Maintenance
is never triggered by requester query text, and its latency/calls are reported as
`binding_maintenance`, separately from source-turn latency. Applications should
schedule it explicitly when immediate historical availability is worth the extra cost.

Source turns have one ordinary model call and at most one repair/history call.
This bounds semantic round trips, not arbitrary event size, current policy size,
host work or token count. V15 generation is capped by
`AMEM_INCREMENTAL_MAX_TOKENS` (default 1024), further limited by
`AMEM_PERMISSION_MAX_TOKENS` (default 3072). Other stages retain their budgets. Oversized/truncated control proposals fail closed within the same budget.
No claim is made that arbitrary documents can be processed in constant time.
Free-form operation descriptions are bounded to 240 characters. Oversized control
requests may therefore need upstream event splitting or an explicitly increased
generation budget; truncation never silently accepts a partial control transition.
The compact `c/p/f/b` output selects source spans and uses host-derived IDs, fixed
operation status, source defaults and mediated-read actions. It does not ask the
model to copy facts or repeat identical evidence checks. All semantic choices
(subject, recipient, scope, authority receipt and applicability) remain model decisions.

## Facts, lifecycle and queries

Candidates retain complete source segments, quantities, conditions, exceptions,
negation and evidence-backed subject attribution. Questions cannot become facts
even if misclassified. Optional replacement/cancellation links are validated
separately: an invalid
link is omitted and audited in the extraction receipt, without discarding a valid
fact. Valid links retain the same subject/evidence checks. Segment coverage is
audited separately from permission completion. It is structural coverage, not
a measured semantic recall guarantee.

The model assesses each recipient's scope independently. A new fact can bind to
existing grants without any control change; one recipient's grant is never copied
to another. Explicit source-grounded replacements/cancellations invalidate older
views for a recipient only after that recipient accepts the new binding. Similarity
alone cannot supersede history. Revocation, task end, expiry, source deletion and
erasure remain enforced by the canonical kernel and final release barrier. Candidate
and disposable cache scrubbing follow the existing logical-erasure contract.

`PermissionQueryService(updater)` is reusable without the GateMem runner:

```python
service = PermissionQueryService(updater)
result = service.answer(authenticated_requester_id, question, approved_processor)
requester_output = result['output']
```

The returned packet also contains host audit metadata; expose only its `output`
to the requester. The trusted application supplies the authenticated requester ID; query text cannot
select an identity or write access state. Retrieval filters access before answering.
The answer model receives only permitted evidence and exact permitted citation IDs.
The existing answer plan checks requested parts, and host-only read-only scope
classification distinguishes missing facts (`no_memory`) from denied access (`refuse`).
Supported parts of mixed questions remain answerable. All consumed inputs are
rechecked before buffered release, including evidence the answer did not cite.

See `examples/incremental_memory.py` for a fresh-store project-coordination example.

## Completion audit

Implementation checklist (final evidence is recorded in
`analysis/incremental-permissions-v15/REPORT.md`):

- [x] Empty initial requester access; authenticated discovery; opaque model identities.
- [x] One combined semantic call for ordinary source events.
- [x] Durable protected facts independent of access, with extraction receipts.
- [x] Model-grounded grants, restrictions, tasks and per-recipient binding decisions.
- [x] Incremental, invalidatable decision cache; no repeated no-authorization reviews.
- [x] Host IDs, calculated binding status and explicit restriction replacement.
- [x] One bounded focused repair; preserve successful independent decisions.
- [x] Binding/extraction failures do not impose unrelated control holds.
- [x] Unresolved controls retain protective holds; expiry/revocation/deletion enforced.
- [x] Recipient-specific replacement/cancellation and late grants over retained facts.
- [x] Queries enforce identity, provide only authorized facts, distinguish missing/denied.
- [x] Domain-independent core and nonmedical end-to-end tests.
- [x] Adapter/CLI defaults, manifests, diagnostics and latency/call-count reporting.
- [x] Historical versions and run artifacts preserved.
- [x] Full regression verification, model availability check, and final fix-list audit.

The engineering latency target is ordinary ingestion near the earlier run's
roughly 5.4 seconds per turn. The implementation reduces serial semantic calls,
but this latency target is not established across the dataset. A real Qwen 27B
project probe passed all ten workflow checks: source turns used 1,1,1,1,2,2 calls,
with a 7.60-second median and 9.98-second mean (5.96–17.35 seconds). This temporary
server used one L40 with online FP8 quantization and CPU embeddings; the historical
run used a different serving setup. These are not same-hardware benchmark results.
The completed checklist audits implementation and validation, not a claim that
full real-model utility or latency goals have been proved. See the report for
medical-prefix results, failures retained during development and exact limits.

Fresh full real-model evaluation with the configured model and judge:

```bash
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 15 \
  --run-dir runs/gatemem-medical-raw-stream-v15-fresh
```

Deterministic functional verification (does not measure semantic quality):

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m pytest security_tests \
  evaluation/gatemem_medical/tests -o addopts='' -q
PYTHONPATH=. /home/thaddus/gatemem/.venv-nb/bin/python \
  analysis/incremental-permissions-v15/measure.py
```

V16 answering reviews each authorized evidence record in a required numbered
relevance slot before composing the current/continuing/retired/conditional plan.
A later single-event summary does not cancel another confirmed booking. These
classifications are internal output checks, grant no access, and add no model
call. Only host-authorized evidence reaches this step. Scoped information denials
use fact-level reviews; withdrawing a whole grant requires an explicit supported
revoke/suspend operation.
