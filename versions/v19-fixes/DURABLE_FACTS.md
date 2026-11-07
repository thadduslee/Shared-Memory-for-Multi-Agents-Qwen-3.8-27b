# Durable candidates and focused permission processing (v14)

Version 14 remains selectable; [version 15](INCREMENTAL_PERMISSIONS.md) is now the default.
This document describes the retained v14 workflow. Use a
**fresh run directory and compartment**. Historical v13 stores, attempts, skipped
jobs and predictions are not migrated or replayed automatically. Select
`--permission-version 13` explicitly to reproduce the previous contract.

## Data and publication

An authenticated source event atomically creates its permission job and a separate
pending extraction receipt. `fact_candidates.py` classifies host-enumerated source
segments, preserving exact source/event IDs, character spans, the verbatim text,
assertion/instruction status, supported subject attribution, optional topic labels
and explicit evidence-backed `replaces`/`cancels` relationships. Context is referenced
by source spans; the inference processor also receives the original source context.
Decimal units and common name/title abbreviations retain their sentence context.

Every segment needs a semantic classification, including nonfactual dialogue and
requests. Explicit question spans cannot become facts even if misclassified.
Requests/guesses are classified as context rather than asserted values. This remains
a semantic inference boundary: structural coverage is **not a measurement of factual
recall or classification accuracy**. A model can still incorrectly classify an
assertion as context. Segment decisions and omissions are audited separately from
permission completion. Valid partial candidates are persisted immediately; only
missing/invalid segments are retried, within a durable three-rejection budget.

The ledger lives in the host's privacy database. Candidates have no memory resources,
vector representations, access edges or requester read API. Unbound candidates
survive restarts and loss of their original turn from the six-event control window.
Permissions never come from candidate existence, topic labels, identity or roles.

Policy inference receives current concise ACL, recent authenticated events and the
protected candidate index. Historical candidate text cannot supply new authority.
The host allocates new record IDs, constrains existing references, and exposes
`replace_restriction` as an explicit operation. Replacement compiles into the existing
immutable restriction history, issuer authority and later-evidence validation.
Accepted restrictions cannot be lifted by an unauthenticated replacement; unresolved
proposals retain protective holds.

For an established activity, the host enumerates worker, requester and beneficiary,
and requests a separate semantic decision for each. When the configured self-use
compiler creates a beneficiary grant, pure control validation previews that existing
mechanism before binding; a new semantic recipient decision is still required.
`binding_status` is calculated by the host. No missing decision is defaulted to allowed.

Every live candidate is reconsidered against each active assignment for its supported
subject (all subjects require explicit attribution review when unknown). Each pair
requires scope fit, subject support, necessity/context rationale and final restriction
assessment. Successful pair decisions can be reused only for the same candidate,
assignment, policy and restrictions. Expiry, ended activities, deleted evidence and
revocation are still checked by the existing lifetime and release mechanisms.

An unbound candidate triggers a focused authorization-gap review. Its diagnosis is
one of missed supported authorization, an existing scope interpreted too narrowly,
binding omission, no supported authorization, or unresolved. Additional activity/grant
operations require authenticated recent support and are revalidated as part of the
complete transition. A gap review cannot remove/replace restrictions. Persisted narrow
grants are never silently widened; history outside the control window cannot serve
as fresh consent. A diagnosis is an audited model decision, not ground-truth accuracy.

Only canonical `bind_information` validation can create exact readable views. Candidate
quotes are not copied directly to the answer model. Model calls run outside database
transactions. All ACL, erasure and readable-view publication remains atomic across the
existing two-store transaction. A canonical operation error identifies the operation
for focused replacement/removal; other successful decisions are retained. Pair/recipient
corrections do not require regenerating successful unrelated semantic decisions. The
complete resulting transition is validated again before publication.

## History, erasure and retrieval

Similarity or later source order alone never supersedes a fact. An explicit relation
needs a same-subject older candidate and an exact supporting quote from the new source.
An unbound replacement cannot change another recipient's projection. After a recipient's
assignment accepts the replacement/cancellation, its older view stops being current
readable evidence; the protected candidate/history remains distinguishable in audit.
Deleting/invalidation of supporting source or attribution evidence invalidates and
scrubs the candidate. Erasure scrubs matching candidates, extraction text and disposable
inference caches; hash-based erasure rules prevent resurrection, including unattributed
copies. `erase_candidates` selects older protected candidate IDs without repeating the
source, subject to the existing authenticated erasure checks. Original source/audit
retention follows the existing logical-erasure contract.

Topics contribute retrieval terms only **after authorization filtering**. They also
appear in protected candidate scope reviews. They never confer a grant, override a
restriction or cause a replacement merely by matching another topic. Graph traversal,
derived-memory dependency checks, pre-model retrieval filtering and final release
checks remain in place. Failure holds are not narrowed or cleared speculatively:
unresolved policy jobs still protect their compartment against unprocessed revocations.

## Measurements and validation

Protected tables `fact_extractions`, `fact_candidates`, `fact_processing` and
`fact_attempts` separate extraction coverage, candidates, semantic work and stage-call
audit. Permission validation records include `authorization_gap_reviews` and
`candidate_binding_decisions`. The adapter's `processing_metrics()` reports candidate,
assignment, binding, omission, failure-hold and semantic-stage call/time counts in the
checkpoint manifest, and stage aggregates appear separately in the run summary. `context_complete` is false when extraction coverage is incomplete,
independently of skipped permission jobs. Existing answer labels/scoring are unchanged.

Coverage status describes model segment coverage, not true semantic extraction recall.
Active assignment counts describe accepted record status; runtime validity and evidence
checks still decide access. Retrieval coverage and final answer quality must be measured
separately. Extra extraction, recipient, binding and gap-review calls can increase cost;
large-prefix throughput and real-model accuracy have not been established here.

Run the offline regressions:

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m pytest security_tests \
  evaluation/gatemem_medical/tests -o addopts='' -q
```

The isolated causal-prefix tests in `security_tests/test_fact_candidates.py` use scripted
semantic decisions and assert actual host publication/retrieval outcomes. They include
the previously omitted medication source, quantities/conditions, late grants, accepted
activities, same-event self-use, per-recipient scopes, appointment cancellation, contact
and voicemail details, restriction replacement, revocation, expiry, deletion, erasure,
partial extraction and live focused corrections. They do not measure LLM quality.
See [the implementation report](analysis/durable-candidates-v14-20261030/REPORT.md).
