# V15 implementation and validation audit

This change implements empty-start, model-driven, incremental permission updates
and requester-filtered answering in the reusable privacy engine. GateMem medical
is an adapter and evaluation input, not the policy compiler.

## What changed

The normal source path has one combined semantic response and at most one focused
repair/history response, with a durable budget across restarts. The model decides
control changes, factual classifications, subject attribution and each recipient's
scope/restriction applicability. The relation index is capped at 32 candidates;
repair inputs retain only selected pending-pair/prospective-grant facts, and output
arrays are bounded by requested segments. V15 defaults to a 1024-token generation
budget; operation descriptions are bounded to 240 characters. Host validation checks evidence, authority,
identifiers, lifetimes and publication. Fixed defaults and the compact output wire
reduce repeated tokens; they do not infer consent from roles or identity spelling.

Facts persist as protected candidates even when nobody can read them. Existing
assignments review only new/invalidated pairs; prospective grants can bind a bounded
shortlist of history in the same response. Overflow stays unavailable and can be
processed by explicit bounded maintenance. Invalid optional factual links are
rejected separately from valid facts. Queries never create permission or trigger
ingestion maintenance.

Control failures and binding failures are separate. Binding failures do not block
previously permitted facts. An exhausted source control failure can be quarantined
using the sender's authenticated control authority and saved task involvement.
Unresolved owner controls remain protective, including against future grants.
A later authenticated authority statement plus an explicit model decision can
resolve a scoped interpretation hold; accepted denials remain intact. Lost
resolution evidence restores the hold. Legacy skipped jobs are not rewritten.

## Fix-list audit

| Requirement/fix | Implementation | Verification |
|---|---|---|
| Empty initial access, no fixture-policy shortcut | v15 rejects initial grants/automatic self-use; raw allowlist; source-prefix identity discovery | bootstrap/opaque-input test, input-boundary sentinel tests, fresh live stores |
| One normal semantic call | `incremental.run`, compact `c/p/f/b` wire | forty fact updates: forty calls; live source traces |
| Durable facts independent of grants | candidate/extraction ledger; independent optional-link checks | late-grant, complete-dose/conditions, invalid optional-link tests |
| Model reasons over entities/tasks/consent | combined input with current/recent context, retained tasks, history, current controls | explicit semantic-decision fixtures and real project workflow |
| Incremental relevant cache | candidate/assignment/task/restriction/evidence fingerprint | repeated history absent, unrelated-recipient restriction, lifecycle invalidation tests |
| Host handles mechanical bookkeeping | opaque IDs, binding availability, exact task receipts, immutable restriction IDs, mediated-read closure | compact-schema tests, delayed acceptance, duplicate-grant and read-only task tests |
| Bounded focused recovery | two semantic responses; accepted progress durable; rejected-only reassessment; pinned authority operations preserved | transport/resume, focused missing-denial append, fresh binding repair, publication interruption tests |
| Over-refusal from broad failed-turn holds | binding-only quarantine; authority-bounded control holds | stranger failure preserves owner facts; owner failure blocks; future grant cannot bypass |
| Dynamic recovery without query mutation | current authority explicitly resolves hold using failed source context | owner clarification, forged actor, restart, deletion and accepted-denial tests |
| Recipient-specific replacements/cancellations | valid source-backed edges become effective per accepted recipient binding | per-recipient replacement and cache tests; real revised-meeting answer |
| Revocation/expiry/deletion/erasure | existing canonical kernel plus release barrier; candidate/cache scrubbing | security regression suite; live revocation and budget exclusion |
| Requester-specific answers; missing vs denied | reusable `PermissionQueryService`; filtered retrieval, host read-only absence assessment | generic query tests, citation/release tests, live authorized answers and denial |
| Domain independence | `incremental`, `control_holds`, `answering` import no benchmark modules | project/parcel/personal tests; executable generic example; real project probe |
| Defaults/diagnostics/history | adapter and CLI default15; call/latency/backlog/quarantine reports; older versions selectable | CLI/input-boundary/resume tests; full offline schedule |
| Audit all fixes | this table, checked design checklist, XML test evidence and raw live traces | final measurements recorded below |

## Validation evidence

Final test counts and schedule coverage are recorded in `validation.json`,
`regression-tests.xml`, `targeted-tests.xml`, and `offline-final-verified/summary.json`.
The full offline run uses explicit no-op test processors: it tests scheduling,
empty bootstrap, boundary filtering, persistence and coverage, not model quality
or utility. Scripted measurements execute real validation/publication/indexing,
but their latency includes fixture schema validation and excludes neural inference.

The successful project probe is `live-project-fp8-release/report.json` and
its exact inputs/responses are in `model-trace.jsonl`. It passed all ten checks:
protected unbound facts; no automatic owner access; no access from an offer;
late-grant historical availability; budget exclusion; new facts under an existing
grant; recipient-specific old-fact retirement; role claims grant nothing;
unrelated access survives a stranger's failure; and owner revocation takes effect.
Source turns took 5.96, 6.46, 17.35, 8.71, 14.94 and 6.49 seconds with 1,1,1,1,2,2
calls (mean 9.98s, median 7.60s). Query timing is recorded separately in the raw
trace and is not included in source-turn latency.

Live validation used the cached Qwen/Qwen3.8-27B checkpoint on a temporary local
vLLM server, one L40 GPU, online FP8 quantization, and the CPU MiniLM embedder.
The older benchmark used a different serving setup; these timings are not a
controlled same-hardware comparison. The temporary model server is shut down
when validation ends; other users' GPU jobs are untouched.

The successful release project probe uses the final implementation. Earlier
failed probes are retained, including the stopped first medical prefix and the
38-turn prefix that exposed oversized repair context. The eight-turn release
medical regression covers that domain's earlier excessive-generation event,
without using evaluator annotations or claiming a checkpoint utility score. The
earlier failed probes exposed real implementation/model-contract defects and
must not be represented as successes.

## Final measured evidence

The full regression run passed **455 tests and 43 subtests**. Its JUnit file
contains 498 passing cases including subtests, with no failures, errors or skips.
The focused v15 suite contains 43 tests. The final offline schedule completed
all 4,294 source turns, 579 checkpoints and 567 boundaries with exact coverage,
no skipped/quarantined jobs and no degraded flag. Its implementation digest
matches the final source files; it uses no-op model doubles and measures no utility.

Scripted 40-event measurement made exactly
40 semantic calls and retrieved all 40 authorized facts, with no historical pair
rereviews; its mean 0.307s includes host work and test-model schema validation.

The final eight-turn medical regression is
`live-medical-fp8-release/report.json`. It processed all 8 sources completely,
with empty starting access, complete extraction, no pending pair reviews and no
remaining control holds. Source turns took 4.73, 7.97, 4.49, 2.65, 25.52, 16.33, 3.87 and
58.30 seconds (median 6.35s, mean 15.48s). The 58.30s turn reached the 1024-token
limit and completed on its one repair. This tail latency remains substantial.
There were no original checkpoints in this eight-turn prefix, so **no medical
utility score** was computed. The earlier 38-turn development prefix stopped on
context overflow; its failed evidence is retained and is not a completed result.

## Limits and interpretation

No fresh full real-model GateMem benchmark or official judge score is claimed.
A causal medical prefix is additional diagnostic evidence, not a full utility
estimate. The earlier roughly 5.4s/turn latency target has not been established
across the dataset. Compact output, caching and the two-call bound limit serial
model work, not arbitrary text size, policy size or generation time. Large backlogs
require explicitly scheduled maintenance; ordinary queries cannot broaden access.

The model can still omit/misinterpret a natural-language control, assign a subject
incorrectly or miss a factual relationship. Canonical validation is not a semantic
correctness proof. Invalid links never retire older facts; structural extraction
coverage does not establish factual recall. Trusted workflow failures with no
bounded subject stay conservatively broad; this prototype does not automatically
resolve those unbounded holds. Connectors and callers must supply authenticated
identities; no external identity provider is implemented here.
