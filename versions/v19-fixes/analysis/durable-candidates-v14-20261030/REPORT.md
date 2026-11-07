# V14 implementation and evidence — 30 October 2026

Implemented in `/home/thaddus/a-mem-variant`, using the actual two-store ingestion,
exact-view and dependency enforcement code in
`/home/thaddus/copy_of_directed_edges/Shared-Memory-for-Multi-Agents-Qwen-3.8-27b`
as the architectural reference. Existing user edits were retained. Historical
run artifacts were read using SQLite read-only mode and were not replayed or rewritten.

## Diagnosis from the recorded run

The host persisted bindings but no independently addressable fact candidates.
`permission_changes` and `new_facts` were separate response arrays; a fact with
`make_available_under: []` produced no readable view **and no durable candidate**.
The accepted medication plan at t054 and cardiology update at t051 had empty
`new_facts`; other dose-bearing turns reported facts with empty assignment lists.
A successful permission job therefore did not establish extraction completeness.

The original privacy database has 781 complete jobs and six skipped jobs in three
compartments. The six final errors are:

| Error | Skipped jobs |
| --- | ---: |
| Missing independent task-recipient checks | 1 |
| Incorrect model-reported binding status | 2 |
| Unknown assignment reference | 1 |
| Reusing an immutable restriction ID | 1 |
| Assignment without matching established activity | 1 |

These are original-run measurements, not results of a new benchmark. The supplied
utility result remains 1/38; no replacement full-run accuracy is claimed.

The authorization cases need different treatment:

| Case | Evidence and treatment |
| --- | --- |
| Supported authorization missed | A particular authenticated request/acceptance may support narrow responsive instructions under the configured convention. The live gap reviewer can propose the missed activity/grant, followed by full authority validation. A regression resolves this in the triggering event with one permission attempt. |
| Existing authorization interpreted too narrowly | Review precise scope and supported activity semantics. A focused diagnosis may support a new narrow assignment only with authenticated available control evidence. Persisted grants are never silently widened. Real-model correction rates are unmeasured. |
| Fact not bound to an applicable grant | The host now enumerates candidate/assignment pairs; later facts and historical candidates receive explicit scope/subject/restriction decisions. Same-event beneficiary grants reuse the existing validated self-use compiler. |
| No authorization under policy | Keep the candidate protected, record the diagnosis and publish no view. Roles, kinship, identity and ownership do not substitute for consent or accepted activity. |

For example, before Nadia's t053 dose-bearing pain-medicine reply, the active saved
grants cover blood-pressure monitoring/logging and appointment scheduling. Those
scopes cannot simply be stretched to include pain medicines. The preceding specific
patient request is relevant support to review under the configured convention.
Conversely, a clinician's medication statement alone is not a patient read grant.
These examples are causal diagnosis, not a claim that every old refusal should
become an answer.

The older approach's 31/38 utility result uses fixture roles, relationships and
subject mappings. That is a separate authorization assumption mismatch. We did
not import it into v14, count its answers as attainable targets, or quantify
"unsupported by raw policy" cases without a complete supported authority review.

## What changed

- A separate extraction receipt and host-only JSON ledger retain source-grounded
  candidates independently of grant/binding success. Exact spans, full quantities,
  conditions, negations, subject support and assertion status remain addressable.
  Partial successful segment extractions survive focused correction of omissions.
- Explicit per-recipient inference controls publication. All relevant active
  subject assignments are assessed; later supported facts reuse existing grants
  and lifetimes. A bounded gap review records the four requested failure categories
  plus unresolved cases, without manufacturing permission.
- Host ID pools, constrained references, recipient enumeration and calculated
  binding status remove bookkeeping from semantic output. Restriction replacement
  creates a fresh host ID and preserves the existing immutable authority history.
  Canonical semantic errors receive focused operation correction; successful
  unrelated pair/recipient decisions are cached and retained. Full transitions
  are revalidated before atomic publication.
- Explicit replacement/cancellation relations require source evidence. Topic labels
  organize protected reviews and authorized retrieval only. Similarity does not
  supersede facts, and an unbound update cannot change another recipient's view.
- Revocation, expiry, source deletion/invalidation, erasure, graph/dependency access
  and final release checks remain enforced. Candidate text and disposable caches
  are scrubbed by the corresponding lifecycle operations. Necessary unresolved
  holds are retained rather than cleared to improve utility.
- Extraction, authorization-gap diagnosis, binding review, holds and retrieval
  availability have separate audit/summary measurements. Answer scoring and labels
  were not changed. V14 is the fresh-run default; explicit v13 remains available.

The complete contract, storage semantics and commands are in
[../../DURABLE_FACTS.md](../../DURABLE_FACTS.md).

## Measured isolated evidence

[measure.py](measure.py) reproduces 11 isolated causal-prefix probes using **scripted
semantic decisions**, actual source ingestion, canonical validation, SQLite
publication, authorization-filtered retrieval and final release checking.
[measurements.json](measurements.json) records each stage separately.

Across the positive retrieval probes, all **18/18 explicitly asserted evidence
items** reached authorized retrieval: omitted plan doses and maximums; medication
exceptions; appointment cancellation/new time/arrival; contact number and voicemail
restriction; per-recipient responsive instructions; and same-event/late grant facts.
This is compiler/retrieval evidence under supplied decisions, not benchmark accuracy,
semantic extraction precision/recall or real-model grant-inference quality. The
fixture processors can classify extra context as instructions; candidate counts
must not be read as factual precision scores.

Revocation leaves zero active assignments and zero retrieved records in its probe.
Deletion scrubs the candidate and leaves zero retrieved records while retaining one
necessary source-reassessment hold. Erasure of an unbound historical candidate
works after the source leaves the control window. The live missed-grant probe and
same-event self-use probe complete in their triggering permission event; they do
not replay historical skipped jobs. Stage call times are local scripted overhead,
not model latency. Final answer-model accuracy is **unmeasured**; release validation
is measured separately.

Validation completed:

- Complete security and evaluation suites: **410 tests and 43 subtests passed**.
- Final changed-boundary selection: **45 tests passed**.
- Additional runner/integration selection: **82 tests and 27 subtests passed**.
- The complete suite includes **26 focused v14 causal-prefix/security tests**.
- Python compilation and `git diff --check` passed.

The selections overlap; their counts must not be added together. JUnit artifacts:
[complete regression suite](regression-tests.xml),
[changed boundaries](final-boundary-tests.xml), and
[runner/integration](final-integration-tests.xml). Machine-readable results are in
[validation.json](validation.json), and the final implementation hashes are in
[implementation.json](implementation.json).

## Remaining limitations

The real model endpoints on ports 8000 and 8001 both refused connections; the NVIDIA
driver was unavailable. No real-model causal-prefix evaluation, new full benchmark,
answer quality improvement, throughput or cost improvement was measured. Additional
extraction and focused semantic calls may increase processing cost.

Natural-language assertion classification, subject attribution, scope fit, authority
meaning and replacement meaning still depend on semantic inference. Complete
segment coverage cannot prove factual recall. Very large candidate histories and
mixed-scope single sentences need real-model cost/recall assessment. A genuinely
unsupported grant stays absent. An incorrectly narrow historical grant is not
automatically widened using protected fact history as consent.

V14 does not clear the historical six skipped jobs or change their recorded results.
A remaining unresolved semantic rejection continues to hold access when the source
could contain a revocation. Model-backed validation and a fresh raw-stream benchmark
remain necessary before claiming an accuracy improvement.
