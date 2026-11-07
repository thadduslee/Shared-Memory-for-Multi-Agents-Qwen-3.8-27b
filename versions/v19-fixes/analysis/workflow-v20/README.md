# V20 bounded recovery and useful, privacy-checked answers

V20 is opt-in with `PermissionUpdater(..., version='20')` or
`--permission-version 20`. V19 remains the default. Use a fresh store/run;
historical scope labels, frozen sources, predictions and scores are not migrated
or rewritten. Existing workspace changes made before this implementation remain.

## Behavior

1. A trusted connector attests stable person IDs. Names are display text, not
   merge keys. Model aliases (`p1`, `own1`, `g1`, `t1`) are references to those IDs;
   they cannot create authority. Each operation supplies its legal references.
   An ID is not a credential: the application must authenticate the requester
   before passing their ID to `PermissionQueryService`.
2. A source gets at most two foreground completion calls, including independent
   exact-fact attribution/scope review. Valid control, factual and binding
   sections are retained independently when another section fails. Unknown
   subjects stay protected rather than generating a reader-by-fact cross product.
3. An unresolved control produces a protective hold and durable host recovery
   work. `updater.service_pending(seconds=5)` services one recovery/maintenance
   batch. The two additional semantic recovery attempts are shared across control,
   attribution and missing-extraction repairs for the same source, across restarts.
   Payment-declined requests do not spend that semantic budget. Exhaustion is
   `needs_review`. No timer clears a hold.
4. Recovery reads the original authenticated prefix. A validated reassessment
   can clear that event's hold without another user statement. An old control
   delta is conservatively rejected after later policy operations; it cannot
   override a later revocation or erasure. Interrupted child jobs never reenter
   the foreground queue. Local compartment file locks serialize publication
   across connections/processes and are released by the OS after crashes. Hold
   resolution and reassessment publication commit atomically. Read-only missing
   extraction is recovered before later source events; historical extraction or
   attribution stays protected for review rather than reviving a stale fact.
5. Accepted-worker service scopes retain original worker/subject receipts.
   Generated initial-topic hints are not closed field lists. Explicit source
   exclusions and lifetimes still constrain every fact. Roles, summaries and
   queries grant nothing. Personal OWNS access remains subject-bound.
6. Facts record all disclosed subject IDs. Existing sentence segmentation keeps
   exact source spans; an inseparable mixed-person fact needs a current positive
   grant/review for every person before model-context exposure and at release.
   Focused reviews independently check that attribution is exhaustive. Unknown
   or disputed identity is unavailable, never guessed from a matching name.
7. A host-only query-policy classifier checks sensitive comparisons before
   retrieval. Unauthorized comparisons are declined, offering an ordinary
   authorized-record lookup separately. Permission fingerprints are checked
   again at release for comparisons that were allowed.
8. An ordinary query can prioritize relevant reviews under existing grants. It
   waits at most five seconds for one bounded read-only classification/review batch; it cannot create permissions
   or recover historical controls. Supported plan items, continuing instructions,
   cancellations and exceptions are rendered from verified source evidence.
   Host corrections to answerability do not require another prose-generation call.

Only `result['output']` is intended for the end user. `input_manifest`, processing
diagnostics, queue contents, scope labels and timing details are trusted host
interfaces. They must not be exposed as hidden-fact existence signals.

## Budgets and reporting

Targets are source mean <=15s / p90 <=30s, answer p90 <=20s, utility >=85%,
over-refusal <=10%, and privacy/context/deletion leakage no worse than v19 on the
same first 147 checkpoints. Zero deletion leakage remains required. These targets
have **not** been validated against paid models.

Source and answer work use 30s and 20s deadlines. Pending reviews get at most 5s
inside the answer deadline. Deadlines constrain lock acquisition, network timeouts,
retry admission and publication; late provider results are discarded. Provider
behavior and OS scheduling still need live latency measurement.

Policy-classification and comparison requests use at most 512 output tokens;
answers use at most 4,096. Source/review work remains bounded. The unchanged
generic processor default still supports older versions. Transport retries are
reported separately from completion calls.

The evaluation host services one bounded host batch between source turns, within
the source's remaining 30s allowance. Optional enrichment remains outside the
answer path and gets one bounded batch rather than an unbounded shutdown drain.

`summary.json.workflow_v20` reports foreground and source-including-host latency,
answer/host/judge timings, p50/p90/p95/max, completion counts by phase, transport
retries, queue states and reported usage/cost. Total work avoids counting query
maintenance or nested model time twice. Preflight receipts are separate and
included in `all_provider_reported_cost`. Requests without billing receipts are
counted as `unreported_usage_calls`; reported cost is not a provider invoice.

HTTP 402 saves `paused_budget` and returns exit code **75** without a crash
traceback. Restore the provider/key allowance, then resume with the same manifest.
A successful operation clears the pause while preserving event history. A
preflight-only budget failure has no native evaluation journal yet: repeat the
fresh-run command after restoring allowance rather than adding `--resume`.

## Local validation

From `/home/thaddus/a-mem-variant`:

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m pytest \
  security_tests evaluation/gatemem_medical/tests -q \
  --junitxml=analysis/workflow-v20/local-tests.xml
```

The new tests use scripted completions, fake HTTP responses and fake clocks.
The recorded local result is **637 passed, 0 failed, 0 errors, 0 skipped**; see
`local-validation.json` and `local-tests.xml`.
They cover invalid IDs, independent sections, restart/crash budgets, publication
locks, historical revocation/erasure, accepted-service follow-through, mixed-person
facts, the four recorded privacy scenarios, pending waits, partial answers,
long-episode cache reuse, token limits and budget pause handling. Existing suites
exercise closed fields, conditions, deletion timers and release barriers.

An offline adapter smoke is functional validation, not model-quality evidence:

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -m evaluation.gatemem_medical \
  --permission-version 20 --offline --no-evolution \
  --run-dir /tmp/amem-v20-new-smoke --max-checkpoints 2
```

The live runner and OpenRouter wrapper reject v20 live/preflight requests unless
`--allow-paid-api` is explicitly supplied. No paid calls were authorized or run
for this implementation.

## Live evaluation on the existing local API

The user subsequently authorized a live tmux run through
`http://localhost:19000/v1`. `local_session.py` freezes the source and restricts
both provider URLs to loopback. It uses `qwen3.8:27b` for memory/answers and a
separate `qwen3.6:35b` judge, with the existing embedding snapshot on CPU. It
starts no model servers and does not configure OpenRouter. The initial Gemma run
failed its real seven-item scoring contract and remains preserved.

Source processing uses JSON mode with the full contract supplied and validated
locally. Small reviews use `portable` constrained output: only the unsupported
`uniqueItems` grammar keyword is projected out of the provider schema, and the
full original schema is still enforced locally. The judge uses its own strict
schema and a 4,096-token limit. Those settings are pinned separately in the
manifest. Large constrained source grammars were probed and rejected for this
launch because they added substantial latency.

Judge retries receive persistent, field-specific feedback under the original
three-attempt budget. Exhaustion saves predictions and exits with status 76
(`paused_contract`) without a crash traceback. Invalid verdicts never become
scores. Repairs cannot change stored predictions.

The source call omits provisional binding reviews that would immediately be
discarded for independent verification. Focused batches shrink from a maximum
of eight pairs to fit the remaining deadline; redundant current grants for the
same reader AND subject do not need repeated review. Every disclosed subject
still requires authorization. Existing ownership uses a separate worker-service
operation without redundant self grants. Control recovery reassesses the
original controls without re-extracting historical facts, and may use the
source's remaining allowance instead of an invariably short five-second slice.
Queries retain their five-second pending limit. A 250ms reserve allows final
publication within the existing source/answer targets; OS scheduling can still
affect measured wall time.

The revised implementation passed **648 local regression tests**; see
`restart-local-tests.xml`. Real judge probes passed the largest nine-item
utility contract, privacy, and deletion contracts; see
`restart-live-preflight.json`. These probes validate provider compatibility,
not candidate accuracy or leakage. The earlier 84-test provider-only result is
retained separately. Full model traces (including rejected drafts), process
status, exit status and live progress are saved alongside native artifacts.

The later six-source compatibility probe hit provider timeouts, including
15-second timeouts for trivial greetings on both local models. Its temporary
store is retained and its failed result is recorded in
`restart-source-preflight.json`; it is not an official candidate run. The
supervisor now waits for both models to pass a five-second, 32-token readiness
probe before starting the source engine. Failed availability probes back off to
one per minute and do not consume source-event recovery budgets or create
canonical holds. Permanent HTTP rejections pause visibly; 402 returns 75 and
other configuration rejections return 78. Probe requests and receipts are
recorded in `provider-preflight.jsonl` and included in workload reporting. The
progress window shows `waiting_provider` while no source worker is admitted.
Startup admission does not guarantee the provider will remain responsive later.

The runner's existing `--allow-paid-api` flag is required as its live-call
opt-in, including for localhost; it does not select OpenRouter. Local-service
billing, if any, is separate. Provider/judge changes mean this run cannot pass
the model-matched v19 comparison gate, even when checkpoint order matches.

```bash
PY=/home/thaddus/gatemem/.venv-nb/bin/python
$PY analysis/workflow-v20/local_session.py prepare /absolute/fresh/run
# Run these frozen commands in separate tmux windows:
$PY /absolute/fresh/run/frozen-source/analysis/workflow-v20/local_session.py run /absolute/fresh/run
$PY /absolute/fresh/run/frozen-source/analysis/workflow-v20/local_session.py watch /absolute/fresh/run
```

Add `--resume` to the frozen `run` command after a stopped run; preserve its
configuration and source. Review `progress.json`, `summary.json`, `run.log` and
`exit-status.json`. Partial scores are not full-run validation.

## Paid OpenRouter validation: prepared, not executed

Run only after separate authorization, against a new run directory. Preserve the
tested source revision while a run is in progress; the manifest pins its digest.

```bash
/home/thaddus/gatemem/.venv-nb/bin/python -u \
  -m evaluation.gatemem_medical.openrouter \
  --permission-version 20 --allow-paid-api \
  --run-dir /home/thaddus/a-mem-variant/runs/gatemem-medical-v20-approved
```

The existing private `.env` supplies credentials; no key is copied into commands.
The wrapper retains the memory and judge models used by v19. Add `--resume` only
for an existing native run with the same configuration. An optional
`--max-checkpoints 147` stops at the exact diagnostic prefix; after review, resume
the same run without that limit to finish the schedule.

The read-only comparison command reports the matched prefix and full results
separately, checking both answer and context leakage:

```bash
/home/thaddus/gatemem/.venv-nb/bin/python analysis/workflow-v20/compare_runs.py \
  --baseline runs/gatemem-medical-v19-openrouter-full-20261102T005006+0800 \
  --candidate runs/gatemem-medical-v20-approved
```

A partial run cannot pass the full-coverage gate. Models/dataset and checkpoint
order must match. Full-run leakage thresholds use the v19 reference rates, but
case composition differs; the matched comparison remains the direct comparison.

## Limits to review before release

- Local tests verify contracts and enforcement, not live semantic accuracy or
  latency. The 85%/10% targets remain unverified until the authorized benchmark.
- Attribution, accepted-service interpretation and comparison detection still
  depend on model judgments. IDs prevent name-based merging; they do not prove
  which person a natural-language fact describes.
- A stale historical control/factual delta, exhausted repair, or ambiguous mixed fact can
  remain `needs_review`. The implementation preserves that boundary rather than
  silently widening access. It does not provide an operator UI or reset budgets.
- The host scheduler is explicit and bounded, not an unmanaged background thread.
  Use a SQLite connection and processor instance per owning execution thread;
  stores and advisory lock files must be on a local filesystem.
