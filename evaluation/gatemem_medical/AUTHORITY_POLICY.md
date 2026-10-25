# GateMem medical authority and interpretation policy

This is a benchmark adapter convention, not a hospital authorization standard.
The domain-free core does not know staff roles, patients, clinical terms or
GateMem query categories. A production source must supply its own verified ACLs,
assignments and approval events instead of inheriting this convention.

## Trusted inputs

`asker.principal_id` authenticates the simulated query identity, and only
`entities.principals` supplies role metadata. A role claimed in query text is
irrelevant. Explicit subject bindings in `fixture_policy.json`, relationships,
and the available authenticated source prefix define the simulated encounter.
Requester-visible source IDs and compartment/policy references hash the episode
key, so fixture filenames do not disclose diagnoses through metadata. Original
source keys remain in the operator-only ingestion journal. First names never merge patient compartments; ambiguous/multiple subjects keep
whole-source guards and receive no operational projections.

The configured authority is `gatemem-authenticated-task-workflow-v1`. It interprets
a recognized, authenticated staff **task performance** in the primary subject's
encounter as a workflow receipt that activates only its listed information
scopes. This is an explicit trusted adapter convention: the source supports
involvement; the convention supplies the task authority. It is not a claim that
speech, authorship or occupation universally grants access. Greetings, questions,
requests for access, unknown staff, secondary/ambiguous subjects, and claims such
as “I only see …” cannot activate assignments. Assignment records retain the
convention ID, issuing service, source observation/turn ID and basis.

| Established task | Eligible information scopes |
| --- | --- |
| Scheduling | appointments, arrival, changes, parking; ambiguous mixed clinical preparation stays unresolved |
| Pharmacy | medication/counseling fields and primary operational contact fields, unless explicitly narrowed |
| Nursing care | care instructions/precautions, medication, primary/restricted task contact and sharing status, appointment/order fields and bounded clinical/laboratory recaps |
| Front desk | contact, sharing status and appointment/arrival fields |
| Social coordination | primary/restricted contact routing and sharing status; transport plan only after patient acceptance |
| Laboratory | existing orders and laboratory scheduling; no result/medication/diagnosis grant |

A task receipt does not give access to unrestricted base notes. Assignments bind
only matching subject/information-scope field resources. Field extraction is a
closed deterministic compiler over exact source character spans, not a model
“sanitizing” a chart. Unknown/mixed sentences may be omitted to preserve the
boundary; this limits recall. A pharmacy restriction such as “medication-only”
revokes the contact assignment and cannot be reversed by later staff assertions.
Episode 018 t130 remains unresolved without independent authorization.

Transport offers alone do not authorize the transport plan. The adapter requires
patient acceptance in an already available adjacent source prefix. It records
both offer and acceptance IDs. There is no blanket staff care-group enrollment.

Assigned-clinician relationships retain the existing trusted care-group behavior.
Covering relationships are handled separately: only explicitly mapped scopes,
source activation, and validity through the end of that source day may create field assignments. Unknown
scope language remains in internal diagnostics. A prior coverer's historical
statement is not current authority. Consulting and unsupported delegation language
remain unresolved rather than granting whole-chart access.

## Sharing and source lifecycle

Family permission uses a separate logistics field ACL and live membership.
Family views use typed exact selectors for grouped appointment times, statuses
and approved generic locations/parking. Specialty and procedure labels are
withheld. These structured labels are deterministic adapter metadata, not
generated factual summaries; every selected value retains its source span.
Contact numbers, clinical results and diagnoses never follow from logistics
membership. Negative control language must be scoped to the information it
restricts; a prohibition on sharing a callback number does not cancel an unrelated
ride permission. Revocations take precedence over stale calendar evidence.
Ambiguous future revocation language is handled conservatively, not used to
extend access from an unsupported date interpretation.

Primary operational contact excludes housing/shelter/backup-site fields; those
use a distinct restricted-contact scope for established coordination/nursing
workflows. Pharmacy, front desk and covering grants do not inherit that scope.

A source-addressed routing view can show “family access revoked” or the permitted
routing instruction without exposing policy rule bodies. Field-audience approval
is given by this adapter's trusted source authority before indexing. A view keeps
lifecycle lineage to its source without requiring whole-document read authority.
Deleting a source cascades to its views and generated descendants.

Deletion uses authenticated patient/controller instructions and exact
source-derived identifiers. Hashed selectors in the privacy database prevent
subsequent source turns from reintroducing the erased values into searchable
notes. Deletion confirmations are recorded without the old values. Unknown
selectors become diagnostics; the adapter does not erase every phone number in
an encounter as a substitute for identifying the requested field. Stored source
text redacted at ingestion records its transform and original source digest.
This is logical deletion and index removal, not physical erasure of fixtures,
SQLite pages/journals/backups, saved evaluation files or delivered output.

Source requests/questions remain auditable observations but their notes are
non-evidentiary and unindexed. A guessed number in a recovery request is not a
factual memory of that number. This limits recall of useful questions; it is
preferable to treating a request as its own supporting fact.

## Forbidden inputs and unresolved authority

`expected_action`, `query_type`, `judge_spec`, `leak_targets`, `policy_snapshot`,
gold answers, judge output and future turns are never authorization inputs.
Visible queries cannot write to memory or assignments. Retrospective cohort
selection and scoring occur only after retrieval in `access_audit.py`.

Internal diagnostics require the issuing authority/operator session. They record
unknown/ambiguous subjects, unsupported relationship windows, self-asserted scope
extensions and unresolved erasure selectors. Public failures remain uniformly
unavailable; diagnostics are never appended to a denied answer.
