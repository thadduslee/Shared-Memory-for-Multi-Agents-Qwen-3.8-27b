# V19 personal statements, patient context and explicit read consent

Changes in `/home/thaddus/a-mem-v19-fixes`:

- Ordinary first-person states and personal disclosures identify the authenticated
  speaker without requiring special phrases such as “my own.” Clear self
  disclosures receive a subject tag even when the model omits one. Quoted first
  person text and staff actions such as filing somebody else's records do not
  establish personal ownership.
- An otherwise complete follow-up note can use a personal statement by its
  identified subject in the existing six-event window. The original statement is
  attached as evidence even if the model cited only the follow-up note. A different
  person's intervening personal disclosure closes that context. Missing, ambiguous
  and mixed-person attribution is still protected; no patient is guessed from an
  episode ID, role, surname or benchmark metadata.
- Explicit permission such as “Tess may read my appointment time and location”
  takes the literal field-grant path without requiring a worker offer. If the
  model mistakenly proposes a service operation, the existing bounded retry may
  correct that operation before the mistake becomes a durable service acceptance.
  For literal read permissions, the output grammar excludes self/worker activity
  operations so the model cannot substitute another self grant for the named
  recipient. The prompt explicitly requires both the fields grant and any
  independent denial. Existing scope, authority, denial, expiration and revocation
  checks still apply.
- Exact unique citations have their character positions calculated by the host
  before independent verification. Unknown IDs, altered/ambiguous quotes and
  unsupported claims still fail. A covered verifier response must include its
  support groups in the output grammar, matching the existing host check.
- The ownership prompt is included only when personal ownership is enabled in
  the trusted configuration. The source prompt revision is now
  `permission-inference-v19-disclosed-subjects-v2`.

The existing query service already checks the requester's current permissions.
These fixes make the subject association and explicit grants available to those
checks; they do not add permissions when someone asks a question.

## Validation artifacts

`attribution-tests.xml` and `acceptance-tests.xml` record focused end-to-end
regressions, including the cases found by the local model check. The broader
regression output is `regression-tests.xml`.

The broad command excludes two V20 test modules whose required V20 implementation
is absent from this V19 checkout. It also excludes one legacy test which expects
“fresh compartment/store” where the existing error says “fresh store/compartment.”
The same assertion failure was independently reproduced using the preserved,
unchanged source snapshot; see `baseline-wording-failure.xml`.

`live-check.py` uses the local Qwen3.8-27B and MiniLM models on a fresh, synthetic
seven-turn conversation. It checks patient symptom/plan answers, family access to
appointment time/location, and protection of symptoms from that family member.
`live-check.json` preserves source processing and the answer failures that
identified the citation-formatting issues. The final four query checks in
`answer-recheck.json` all pass: patient symptoms, patient plan, permitted family
appointment details, and refusal of private symptoms. The source store and its
semantic budgets were preserved for that query-only recheck; its only runtime
change was `answer_validation_v19.py`. `validation-summary.json` verifies the
final runtime hashes and records all test reports. This is a bounded compatibility check, not a full benchmark or an
independent clinical judgment. Earlier setup/intermediate attempts are retained
in their own directories.

Historical benchmark predictions, scores and exhausted repair budgets were not
rewritten. A full evaluation of the changed code requires a fresh run.

Final checks: 890 broad regressions, 122 focused source/ownership checks, 130
permission checks and 55 answer-validation checks passed. These test sets overlap;
the counts should not be added. The broad pass preceded the final refinements,
which were covered by the focused checks and the actual-model recheck.
