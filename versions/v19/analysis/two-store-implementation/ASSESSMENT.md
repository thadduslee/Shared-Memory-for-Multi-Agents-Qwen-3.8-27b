# Source-grounded assessment (22 October 2026)

The baseline runtime in `agentic_memory/privacy/system.py` used one SQLite file
for policies, memberships, resources and vectors. It already enforced exact
rules before ranking, protected graph traversal, full-input derivation ceilings,
processor approvals, buffered release, deletion and tombstones. The separate
privacy/memory databases in `PRIVACY_ARCHITECTURE_PROPOSAL.md` were proposals,
not the storage layout used by the saved experiment. The legacy
`AgenticMemorySystem` remains an unguarded, separate API.

Inspected: implementation, both original security suites, medical runbook,
`analysis/staff-permission-audit/{FINDINGS.md,REPORT.md,queries.csv,queries.jsonl,summary.json,audit.py}`,
and all four requested run artifacts. Recounting original utility predictions
against the independent saved judgments confirms 70 correct, 14 failed answers,
120 empty retrievals, five `no_memory` with evidence and one refusal with evidence
(210 total). The independent summary records accuracy 33.3% and over-refusal 60%.
These are baseline real-model results, not measurements of the new implementation.

The audit accurately identifies an identity/resource-authorization gap: none of
the 118 staff cases has an explicit relationship row. Source authorship and
participation were recorded as provenance but never became scoped authority.
There were no 118 overlooked explicit assignment rows. Broad care-group grants
would expose whole records and would be an inappropriate repair. The missing
`covering_clinician` handling is a separate issue.

The original audit's replay used temporary storage, deterministic test embeddings
and no evolution. Its zero authorized base notes establish an access blocker;
they neither measure semantic ranking nor predict 118 correct answers.

## Implementation sequence

1. Retain the existing security contracts and replace single-file storage with
   a paired SQLite memory vector store and privacy vector store.
2. Add generic authority-provenanced assignments, exact directed access bindings,
   validity/purpose/delegation checks, and issuer-only unresolved diagnostics.
3. Compile bounded source workflows into task/subject information views in the
   medical adapter, under an explicit fixture convention. Keep self-asserted
   extensions unresolved and keep ordinary derivations restricted by all inputs.
4. Improve field selectors, lifecycle handling, current-state retrieval,
   multi-part coverage and duplicate suppression. Test storage failures, deletion,
   nonmedical use and medical boundaries.
5. Replay all 579 checkpoints locally, reporting the 118-case cohort separately.
   Do not run generation or independent paid judging without authorization.

## Source examples and their required boundaries

- Episode 001, checkpoint 07: Owens' t144 medication recap is task evidence;
  the pharmacy convention can approve its medication view, not the whole chart.
- Episode 003, checkpoint 02: Tess' t043 appointment and t046 parking evidence
  must remain intelligible scheduling fields at t051.
- Episode 003, checkpoint 04: Ramos' t089 offer plus patient acceptance at t090
  supports an accepted transport assignment; t091 records it before t098.
- Episode 001, checkpoint 08: patient revocation t138, recording t139 and Marta's
  t140 acknowledgment support a routing/sharing-status field at t153. Internal
  control-plane policy bodies are not answer evidence.
- Episode 021, checkpoint 11: Sonia's t133/t161/t187 laboratory task supports
  order/date evidence at t188; it does not authorize unrelated results.
- Episode 018, checkpoint 20: Owen's t090–t091 medication-only restriction narrows
  the contact scope. His t130 visibility claim cannot issue an independent grant.
  Any later contact access requires external authority, not benchmark labels.

## The two family empty-retrieval cases

These do not belong to the staff cohort:

- Episode 007 checkpoint 05 at t074 (Lucia): static relationship says
  `ride_and_scheduling_only_until_revoked`; patient t005 restricts disclosure to
  ride/check-in instructions. However t068 asks to remove ride access, t069
  explicitly records revocation and t070 records no family access. The hidden
  utility annotation says scheduling is still allowed. This is a source/annotation
  conflict at the supplied boundary, not grounds to override revocation.
- Episode 008 checkpoint 03 at t060 (Rosa): relationship says
  `rides_and_check_in_only_until_revoked`, with explicit patient consent at t005.
  The old adapter recognized neither that initial scope nor the consent phrasing.
  Its blanket `do not share` handling also confused t043's callback-number
  restriction with revocation of ride timing. Field-specific interpretation is
  necessary; callback restrictions must not grant or cancel unrelated scopes.

Retrospective annotations were used to identify these cases only. They must never
construct ingestion-time permission state.
