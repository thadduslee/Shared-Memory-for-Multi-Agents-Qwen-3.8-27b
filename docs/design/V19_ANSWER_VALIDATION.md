# V19 answer evidence validation

Workspace `/home/thaddus/a-mem-v19-fixes`, branch `v19-recipient-revocation`.
Base `5672313`. This follow-up preserves Batches 1–5 and the existing Batch 6
task-contract/host-event implementation. Its audit, `V19_BATCH6.md`, is not
overwritten. Historical runs and V20/V21 workflows are unchanged.

## Enforcement and repair

Version family **19** now enters `answer_validation_v19.py` before the older
ScriptedProcessor shortcut or exact-record renderer. Default mode is **enforce**,
including offline callers. Other permission versions retain their existing path.

1. Retrieve once through the existing requester, subject, binding and hold gates.
   Check packet ID membership in the full processing manifest and exact equality
   with current readable resource bytes. Do not read a protected raw document.
2. Generate one closed proposal of request parts (`rN`) and atomic claims (`cN`).
   Validate unique IDs, request references, exact evidence IDs, character spans
   and quotations. No unrestricted final prose is accepted from this generator.
3. Make **one independent verification pass** over only that authorized packet,
   proposed components and their deterministic proposed final rendering. The
   default uses the same backend with a separate stateless system prompt, not a
   claimed different-model ensemble. A host-approved separate verifier is optional.
4. Check relevance, entire-claim entailment, and current-state consistency for
   every component. Subject, category, quantities/dose, time, negation, conditions,
   explicit cancellations/replacements and continuing instructions are included.
   Request-level coverage groups identify all necessary supported parts and
   qualifiers; omitted query parts are reported separately. No outside domain
   knowledge fills evidence gaps. Query/evidence/answer instructions are JSON data.
5. Repair locally by choosing only passing components with applicable request
   coverage receipts. Prefer faithful paraphrases over equivalent quote options.
   Bounded whole-record quote alternatives are supplied before verification, so
   a missing condition/continuing instruction can be restored without regeneration.
   They undergo the **same** relevance, entailment and current-state checks.
   Exact quotation alone never establishes relevance or a valid categorization.
6. Render only unchanged certified components/categories. Validate the actual
   final JSON, deterministic composition and citation spans. No later free-text
   renderer runs. `retrieved_memory` contains only selected support spans; rejected
   records cannot return through a downstream evidence display.
7. Reauthorize **all consumed inputs**, including uncited/rejected evidence and
   graph dependencies, at the existing buffered release barrier. Revocation,
   erasure, expiry, holds and attribution corrections still prevent release.

This is one bounded selection repair, with zero regeneration calls. The verifier
checks all initial and repair components; the host checks their actual final
composition against those certificates. It does not ask an unchecked second
generator to rewrite rejected output. Invalid/failed verification returns a
generic no-memory result without factual prose, new retrieval or permission changes.
Semantic verifier correctness itself still needs real-model validation.

## Schemas, prompt and final output

Canonical code: `generation_schema`, `GENERATION_PROMPT`, `verification_schema`,
`VERIFICATION_PROMPT`, and `final_output_schema` in `answer_validation_v19.py`.
Exact prompt text and illustrative closed schemas are under
`analysis/v19-answer-validation/`. Dynamic evidence/request/claim enums are rebuilt
for each packet; exported IDs are placeholders, not authority.

Proposal: `requests[{id,text}]`, `claims[{id,request_ids,text,category,evidence}]`.
Each evidence reference is `{evidence_id,start,end,quote}`. Offsets are zero-based
Python character offsets into **authorized projected text**, not raw observations,
bytes or tokens. Exact-span validation does not require a verbatim claim: the
verifier can approve faithful paraphrases and explicitly supported normalization.

Verifier: one named slot per candidate with `{relevant,entailed,current,reason}`;
one per request with `{verdict,required_groups,reason}`; plus `rendering_valid`,
`query_complete`, and `unrepresented_requests`. A required group names alternative
components expressing ONE necessary fact/qualifier. Groups cannot reference
rejected components or another request's claim. Short reason codes replace free
reasoning narratives. Host receipts attach supporting evidence IDs/spans.

Released output retains `action`, `answer`, `answer_structured`, `used_record_ids`
and `retrieved_memory`. Structured categories are `current_items`,
`continuing_items`, `retired_items`, `conditions_and_exceptions`, and the new
neutral `supporting_items`. Neutral quotation does not establish a diagnosis,
classification or active booking. Empty factual answers use `{}`. The final
digest includes the released support-span list. Clients must not reconstruct
answers from rejected components in telemetry.

## Availability, privacy and modes

Host manifests distinguish supported, partial, missing, irrelevant, erased and
out-of-scope parts. Missing/irrelevant evidence alone never establishes denial.
Only the existing separate, trusted read-only policy classification can establish
out-of-scope access. Its private scopes/receipts never enter either new evidence
prompt. Policy-classifier failure retains verified partial facts and uncertainty.
Host-task proofs participate in its liveness check so a JIT grant is not treated
as absent just because its receipt is not raw dialogue.

Erasure recovery describes the request to recover deleted information, not proof
that a hidden value existed. No recovered value is emitted. Empty packets produce
fixed availability messages without answer generation/verification; public absence
messages never confirm an erased value. Partial verified answers survive missing
or independently denied parts. Denied/erasure parts can produce answer_redacted;
mere absence does not establish denied access.

`PermissionQueryService(updater, answer_validation_mode="enforce",
answer_verifier=None)` configures the host path. The raw adapter and runner also
default to enforce; runner flag: `--answer-validation-mode enforce`. A custom
verifier must be registered and permitted by the session and resource policy.
`shadow` is an explicit ablation: receipts report `enforced: false` and
`shadow_only`. It does **not** activate the semantic fix. Query or evaluator input
cannot choose a mode. Neither new prompt receives private policy metadata, hidden
facts, role rosters, gold answers or evaluator verdicts.

## Bounded cost, observability and migration

Limits: 28 evidence records (the normal 24 seeds plus four graph neighbors),
48,000 evidence characters, an 8,000-character query, 12 request parts,
24 generated claims and up to 28 prechecked quote alternatives.
No fact is truncated to meet a limit. Oversized/invalid packets remain unavailable.
`AMEM_ANSWER_MAX_TOKENS` and `AMEM_VERIFY_MAX_TOKENS` default to 4096, also bounded
by the processor cap. The existing processor request timeout applies; configure
it for the endpoint. No semantic retry loop is introduced.

Normal supported path: **one generation + one verification call**, independent
of record/claim count. Repair: **zero additional calls**. Empty evidence: **zero
answer/verifier calls**. A separate existing batched policy classification may
run for unavailable parts. V19's two-response source-ingestion envelope and
maintenance deferral are unchanged; source extraction/permission prompts are not
modified. An old answer/ScriptedProcessor implementation must implement the new
contract or remain unavailable; there is no unchecked offline quote bypass.

Per-query manifests record mode/version, attempted calls, policy-classifier
invocations, verifier wall time, repair count, rejected claim codes, request/claim
support spans, final digest and validation failures. Summary output aggregates
calls, repair frequency, verifier latency and failures. Rejected claim prose is
not copied into released telemetry. `false_rejection` stays null until independent
retrospective/manual adjudication; the summary reports the adjudicated count and
a null rate when none exist. Offline adjudication may use these compact receipts;
gold/judge fields never feed back into production validation.

For actual false-rejection adjudication, the host may opt into
`answer_validation_observer=callable` on `PermissionQueryService` (or set that
trusted adapter attribute). After authorized release it receives copies of the
authorized packet, original proposed claims, verification certificate and final
output. This permits assessment of the precise rejected wording without leaking
it in requester-facing telemetry. Its return value is ignored, mutations cannot
change output, and observer failure cannot discard a verified answer. The core
creates no persistent source/draft archive by default; any private measurement
sink must follow the deployment's retention/erasure rules. Gold labels remain
offline, never an approval override or production validation input.

No existing store/job is replayed, grant widened or source attempt reset. Keep
the prior receipt, attribution and hold tables and both stores. Task registry and
issuer/key migration requirements remain in `V19_BATCH6.md`. A changed-code resume
is a mixed experiment, not a fresh comparable run.

## Local validation and limits

Focused tests include staging rejection; supported medical/nonmedical categories;
paraphrases/normalization; wrong subject, dose/time, negation, category and stale
appointments; partial answers; conditions, continuing instructions and cancellations;
quote laundering; invalid IDs/spans/coverage; injection; failed verification;
current packet bytes; final output/digest; and revocation during both passes/release.
The preceding disclosed-subject filter is checked before either answer call.

`analysis/v19-answer-validation/replay_cards.py` uses only the saved authorized
answer-call packets/responses for `003-05` and `003-07`, in fresh temporary stores.
It never supplies hidden candidates, future sources, assignment metadata or gold
fields to validation. Scripted retrospective non-relevance labels reject both the
original unsupported answer and its exact-quote alternatives: two calls per case,
no-memory output, and no rejected retrieved records. This is protocol validation,
not a real-model semantic rerun or changed historical scores.

Results, exact commands, local timings and remaining work are in adjacent
`validation.json` and logs. **Real-model extraction/binding/verification quality,
false acceptance/rejection, adversarial robustness and matched latency remain
unmeasured. No V19-equivalent latency is claimed. No paid benchmark was launched.**

Final local results: **50 focused tests passed**; broader compatibility:
**601 passed, 33 subtests passed**, with the known baseline ownership-prompt test
deselected; mocked transport/format checks: **19 passed, 2 subtests passed**.
The final focused run includes two observer regressions added after collection
of the broader suite. The default 24-seed/four-neighbor packet is tested at 28
records with one generation and one verification call. Both saved-packet replays
reject the unsupported answers using two answer-stage calls each. An initial
disk-full run was discarded; the final compatibility run completed successfully.
