# Task-resolution correction and validation, 29 October 2026

The implementation closes the host-side silent-omission path: an affected task
candidate cannot be disposed of by a `reviewed` coverage acknowledgment. Questions
can become durable non-authorizing pending involvement, later acceptance can
resolve that involvement, and a positive new-task decision compiles into its own
narrow grant and exact information bindings. This is a structured semantic
decision plus ordinary host validation, not a rule that turns roles, participation
or questions into access.

The saved v10 failures were reproduced from the original ledger. Scripted tests
demonstrate the corrected control and retrieval paths. Real-model evidence and
its limits are reported separately below. No official evaluation score was
rewritten or new full benchmark run claimed.

## Implementation

- `agentic_memory/privacy/task_tracking.py`: bounded nomination, mandatory task
  reviews, durable pending activities, atomic task-grant compilation, targeted
  fact lookup, focused reassessment and retirement.
- `agentic_memory/privacy/permissions.py`: version-12 integration, snapshot checks
  including task state, current authority versus historical fact evidence,
  durable budgets and atomic candidate/policy/field publication.
- `agentic_memory/privacy/storage.py`: protected `permission_task_candidates` table.
- `agentic_memory/privacy/permission_state.py`: retired tentative activities leave
  the concise inference ACL. Evidence and justifications remain in host audit.
- `evaluation/gatemem_medical/inference_adapter.py` and `runner.py`: default v12;
  explicit v11 and legacy contracts retained. Offline fixtures explicitly declare
  that they make no semantic permission inference.
- `security_tests/test_task_resolution.py`: 35 task-resolution regressions.

Each turn considers at most four candidates: its authenticated speaker, the most
recent different speaker in the six-event window, and at most two affected saved
candidates. This nominates work to assess and supplies no authority. Each review
must include the actual new turn in its evidence and decide `authorized`,
`unresolved`, or `no_authorization`, with a substantive reason and compatible
reason code. Coverage alone does not resolve a candidate.

An unresolved question creates a tentative activity with its original authenticated
worker receipt. The receipt and audit explanation stay host-side; the model sees
the current task and unresolved meaning. No question text is bound as asserted
memory. New subject/authority acceptance can establish this same activity after
the worker leaves the window. Missing acceptance stays unresolved.

For a new supported task, the model supplies an atomic `authorization` containing
scope, configured actions, current corroboration, semantic checks, exact fact
quotes and restriction decisions. Optional validity and current evidence carry
time/event limits. The host links the authenticated worker, compiles activity,
assignment and bindings, and runs the ordinary validators. An established
activity alone, another recipient's grant, a question used as factual memory,
an incorrect quote/source pair, missing restriction assessments or an empty grant
claimed complete all fail before publication. A grant awaiting facts is tracked
as such and cannot expose facts that have not been bound. Resolution clears the
host-staged pending acceptance question from current policy.

The implementation does not parse medical roles, names, benchmark turn IDs or
task keywords. Raw dialogue and the configured authorization convention remain
the source of semantic decisions. Authorship, title and absence of prohibition
do not supply permission. Revocation, deletion, expiry, narrow field projections,
subject boundaries and publication-time checks remain enforced.

An atomic proposal that exactly repeats an active grant's recipient, subject,
activity, scope, actions and validity now reuses that committed grant. New facts
are still validated normally; the original authority lineage and expiry are not
rewritten. Revoked grants are never reused. This avoids a model creating identical
policy rows on successive turns without adding a repair call. The reuse change
was added after the last live run and is covered by scripted regressions, including
expiry and revocation; it was not separately live-model tested.

## Bounded work and storage

Most turns need one call. An unresolved worker candidate followed by a possible
configured subject/authority response can get one focused reassessment in that
job, with the same context. That call asks for a decision and never forces access.
Its receipt persists across errors/restart. Each candidate/responder pair is
reassessed at most once, with at most four responders per candidate. Rejected
proposals retain the existing durable three-rejection budget.

Historical facts are requested only when necessary: at most two lookup rounds,
two queries per round and six older sources total, within the live immutable
source prefix and compartment. No neighbors, prior grant justifications or old
binding history are appended. Old facts may support exact bindings; they cannot
supply fresh consent, revocation or other control changes. Ordinary unrelated
turns do not run an extra history or reassessment call.

At most 32 unresolved/awaiting-facts candidates stay active. Capacity eviction,
32 turns without worker progress or missing worker source makes one dormant.
Dormancy is neither permission nor a denial. A returning worker or known subject
can reactivate it. Archived receipts/terminal decisions remain host audit; the
active inference queue stays bounded. Host snapshot/loading work and the complete
ACL can still grow with real history/grants/restrictions.

## Scripted regression results

Commands run from the repository root:

```bash
python3 -m unittest discover -s security_tests -q
python3 -m unittest discover -s evaluation/gatemem_medical/tests -q
python3 -m compileall -q agentic_memory/privacy evaluation/gatemem_medical security_tests/test_task_resolution.py
git diff --check
```

Security: **273 passed**, including 35 new task-resolution tests. Evaluator:
**53 passed**. Compilation and whitespace checks passed. The newest task-only
run also passed after clearing the resolved host-staged acceptance question.

The new tests cover accepted question/response grants; unanswered questions with
no factual memory/access; corroboration outside the speaker window and after
restart; rejected reviewed/unchanged omissions; missing/wrong-recipient/empty
assignments; atomic exact fact bindings; private unrelated clauses; unauthorized
third parties and other subjects; current-turn evidence; historical fact lookup
versus old authority; restriction suspension; expiry; deletion/revocation;
retirement/capacity; durable lookup/reassessment budgets; reassessment uncertainty;
processor-error recovery; stale candidate snapshots; and publication rollback.

These are scripted semantic proposals. They validate host behavior and enforceable
retrieval consequences, not the real model's ability to interpret every dialogue.

## Saved-trace reproduction and preservation

`retrospective.py` opens both original v10 stores in read-only attached SQLite
transactions and reconstructs checkpoint-time ledger state. At t112 Casey has
no activity/assignment; at t144 Owens has none. The saved accepted t101/t117
proposals fail the new explicit-task-resolution contract. See `retrospective.json`.
This demonstrates that the original silent proposal omissions cannot pass the
new contract unchanged; it does not mean a model replay must infer correctly.

The original diagnostic `REPORT.md`, `trace.json`, `audit.py`, v10 predictions,
scores and manifest match their protected hashes. Original run stores were opened
read-only. All model smoke stores are fresh copies under this directory. The
original audit script was read but not executed because it rewrites its trace.

## Actual model smoke evidence

`smoke.py` uses the existing local `Qwen/Qwen3.8-27B` service and fresh SQLite
stores with `HashEmbedder`. It ingests only authenticated raw source-turn fields
through `source_turn`. No checkpoint question, expected action, judge include
list, hidden relationship or role annotation reaches permission inference.
The retrospective harness selects dialogue subsets using the diagnosis; this
is intentionally targeted testing, not a blind or full-prefix benchmark.

Every run retains its code snapshot, request/response audit and timing. These
development snapshots differ; their results must not be represented as repeated
trials of the final implementation.

| Run | Actual evidence | Result |
| --- | --- | --- |
| `live-smoke-01` | 20 model invocations, 65,506 prompt / 15,344 completion tokens, 972.034 s | Mandatory resolutions alone still missed Casey; counseling hit a transport timeout at t117. |
| `live-smoke-02` | 8 invocations, 18,522 / 3,895 tokens, 524.869 s | Outreach timed out; Owens hit durable repair exhaustion for a supported review with no assignment. |
| `live-smoke-03` | 4 HTTP/model calls for t116/t117/t144, 11,216 / 2,947 tokens, 114.534 s | Owens received a narrow nausea-treatment follow-up grant with t117 accessible. The broader t144 recap was not bound. One focused reassessment, zero rejected proposals, no family access. |
| `live-smoke-04` | 19 HTTP/model calls, 63,117 / 12,067 tokens, 470.603 s | Full selected histories exposed reason-code contradictions and missing assignments during repair. Both cases exhausted repairs. |
| `live-smoke-05` | 30 HTTP/model calls for 18 selected turns, 127,637 / 33,080 tokens, 1,288.579 s | Both workers received grants; Casey's callback grant was future-dated and its callback text remained inaccessible. Owens received the full medication recap under a narrower two-drug scope: a scope inconsistency, not a narrow-scope success. |
| `live-smoke-06` | 10 HTTP/model calls, 34,301 / 5,951 tokens, 232.324 s | Casey exhausted repairs at t099 because the decoder still allowed old-only review evidence. Owens received a valid narrow t117 grant; t144 remained unbound. |
| `live-smoke-07` | 16 HTTP/model calls for 10 selected turns, 64,338 / 12,256 tokens, 480.874 s | Both workers received narrow accepted-task grants and retrieved their patient responses. Callback updates were not bound to Casey; the full t144 recap was not bound to Owens. No current-evidence rejections, one other repair, five focused reassessments, no history lookup or family retrieval. |

Runs 01/02 predate exact HTTP-attempt counting and can include internal transport
retries; their token totals reflect reported model usage, not unknown work done
by timed-out requests. Early runs overlapped and experienced contention. Run 03
proves a narrow accepted-task grant and actual retrieval, **not correction of the
entire medication checkpoint**. Run 04 motivated discriminated reason-code
variants, recipient-specific committed assignment links and the atomic new-task
branch. Run 05 completed both selected histories with eight focused reassessments,
four rejected proposals and zero history lookups. The family probe retrieved no
facts in either store. Its snapshot predates mandatory current-turn review
evidence, the bounded-fact instruction clarification, atomic expiry options and
clearing resolved pending questions; see the saved snapshot for exact code.

Read-only `inspect_smokes.py` exports actual assignment subjects, scopes, validity
and exact bound text to `scope-validity-audit.json`. In run 05 the callback
assignment's `effective_from` is 2026-03-08, after the source-time checkpoint;
contact-instruction effectivity was confused with permission effectivity. The
medication recap includes facts beyond the assignment's explicitly named two-drug
scope. These are semantic failures despite structurally valid records. They
motivated explicit fact-versus-access effectivity and narrow-list projection
guidance. Such guidance is not a mechanical proof of scope compliance. Later
runs are recorded in the accompanying JSON reports. Run 06 exposed a host/decoder
mismatch: the host required current-turn evidence while the decoder allowed old-only
citations. The final decoder pins the task review receipt to the current source;
worker performance, acceptance corroboration and fact evidence remain separate.
This lets semantic reassessment proceed without spending repairs on that format
error. A regression rejects an old-only receipt at both schema and host boundaries.

Run 07 completed all selected turns with no pending jobs. Casey could retrieve
the pad-change/cramp response at t101; Owens could retrieve the nausea-treatment
response at t117. Their narrower scopes did not supply the original checkpoints'
required callback update and broader medication recap. **Neither original
checkpoint is demonstrated fully corrected.** The original zero-grant omission
is replaced by real narrow grants, but ongoing task/scope interpretation and
selection of all necessary facts remain incomplete. This is actual model-quality
evidence for the partial improvement, not a scripted grant injection or a claim
that the checkpoint answers pass. Run 07 uses final code except the later exact
policy reuse change described above; code snapshots retain the exact boundary.

Across all seven development snapshots there were **107 model invocations**,
384,637 reported prompt and 85,540 completion tokens. Exact HTTP counting covers
79 attempts in runs 03–07; early timeout retry work is not fully counted.
`model-costs.json` preserves the per-run accounting. The summed client run times
are 4,083.817 seconds, with overlap in early runs, so that sum is not a single
wall-clock duration. No further live runs were started after run 07.

## Latency and request size

`measure.py` compares a synthetic two-source request with the same current-policy
snapshot, using scripted processors. `measurement.json` reports:

| Measure | v11 | v12 |
| --- | ---: | ---: |
| Instruction + payload bytes | 7,698 | 12,450 |
| Separate response-schema bytes | 19,595 | 27,174 |
| Local Qwen message tokens | 1,569 | 2,645 |

The added **1,076 message tokens** are task contracts/candidate review machinery,
not replayed permission justifications. Token counts exclude the chat template
and grammar/schema compiler overhead. A scripted supported turn used one call
and about 26 ms of host processing; ten unrelated turns used ten calls and about
183 ms total. Host timings are local samples, not a benchmark distribution.
See [a scripted concise current ACL](sample-current-acl.json): no source receipts,
fact-binding history or grant justifications are included.

Live runs show that repairs/reassessment can materially increase both tokens and
wall time. No controlled live v11/v12 A/B comparison was performed. There is no
evidence here that v12 is faster than v11. The fix trades bounded additional work
for explicit task handling while keeping the concise ACL and ordinary six-turn
window; latency and semantic quality remain separate measurements.

## Remaining limits

Schema/reference checks cannot prove semantic completeness, that a negative
reason is correct, or that the model chose the best narrow ongoing task scope.
The model can still leave genuine acceptance unresolved, choose an overly narrow
scope, omit needed factual text or exhaust repairs. Exact repeated atomic policy
is now reused; semantically equivalent differently worded grants can still be
redundant and are not merged by guessing.
It can also misapply information effectivity as a permission delay or approve
overbroad quoted text under a narrower scope, as observed in run 05. Existing
structured self-checks are not an independent semantic scope verifier.
Budget exhaustion leaves requester access blocked rather than granting by default.
Known-subject pending tasks can be reactivated after dormancy; unknown dormant
tasks may need the worker to return. Exact quotes and a six-source historical
budget can leave a valid task awaiting facts. The full original prefixes,
production embeddings, answer-model output and official checkpoint scores have
not been rerun. Successful raw-subset retrieval must not be equated with full
checkpoint correctness or a general model-quality guarantee.
