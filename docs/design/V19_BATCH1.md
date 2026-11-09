# V19 Batch 1: recipient-specific revocation

Continue Batch 2 in `/home/thaddus/a-mem-v19-fixes`, branch `v19-recipient-revocation`.
The implementation baseline is commit `3d0bc0c`; the implementation diff is
`git diff 3d0bc0c..HEAD`. This workspace is intended to carry the subsequent batches.
The original checkout and its uncommitted V20/V21 work were left untouched.

The baseline starts at `b997519`, replaces runtime/evaluation source with the
historical V19 frozen source, and retains packaging, security tests, and other
supporting infrastructure. All 63 files in `evaluated-source.tar.gz` matched the
frozen source byte for byte. Recomputing the historical runner's code digest gave
`065c20ea8b067a02e6281e0c884e1d86073b92d3a9d9b487925a511a0f45d5c4`, matching the
manifest. File hashes and archive verification are recorded in `V19_BASELINE.json`.
V20 runtime files absent from that snapshot are absent from this isolated baseline;
no V20/V21 workflow was imported. All new behavior is gated on version family 19.

## Validated contract

The authenticated event supplies the requesting actor and, when explicitly
attested, the information subject. Otherwise the supported request concerns the
actor's own information resource. The bootstrap must give that actor restriction
authority for that subject. Requesting actor, information subject, recipient IDs,
and assignment IDs remain separate throughout validation and audit.

The host resolves a recipient reference before examining grants. Supported
references are exact authenticated participant IDs, unique names from authenticated
self introductions, explicit recipient lists, me/myself, and kinship supported by
that actor's explicit authenticated relationship statement or a bounded
corroborated exchange: the subject speaks, the relative introduces their name and
relationship, a question explicitly names that relationship, and the SAME
authenticated subject explicitly permits the relative to receive their information.
Different subjects, unanswered questions, or a relative's claim alone cannot
establish this relation. Receipts retain source
IDs, exact character spans, identity evidence, and relationship provenance. Names
shared by multiple identities and missing/contradicted relationships stay
unresolved. An active worker, role, or matching grant is never identity evidence.
No staff exemption or actor-equals-recipient rejection exists.

Every proposed revocation must match the resolved subject, recipient set, actions,
information field, and immediate effective condition. The complete matching active
grant set must be selected; mixed correct/incorrect grants, omitted recipients,
unknown IDs, and a contradictory model-selected recipient cannot publish. This is
checked again through canonical validation at publication, independently of wire
schema validity. Alternative suspensions or same-proposal replacement grants
cannot bypass the scope checks.

Whole-access withdrawal revokes the grant. Partial withdrawal records a host
compiled mask and retains residual actions, fields, validity, bindings, and the
original semantic scope reviews. Reads, searches, derivations, and final buffered
release consult the current masks. Compound facts containing a forbidden field
and facts whose residual scope is unknown remain protected. Existing locked SQLite
publication, snapshot conflict checks, transaction-local authorization cache
invalidation, and ticket invalidation remain in force. A failure during publication
of the second recipient rolls back both recipients. Recipient holds cannot be
cleared by an ordinary later utterance; resolution requires supported original
identity evidence and a matching current withdrawal. Task-authority policy and
other hold-resolution policy are unchanged.

## Validation

Local interpreter: `/home/thaddus/gatemem/.venv-nb/bin/python`.

The focused suite is `security_tests/test_recipient_revocation_v19.py` (40 cases).
The final targeted run passed all 50 selected cases: these 40 regressions and 10
existing ownership cases, with the known baseline failure explicitly deselected.
It tests
Linda/Jules separation, explicit staff targeting, multiple recipient lists,
multiple grants and scope preservation, ambiguity, missing/contradicted kinship,
self references, authority, cross-namespace/wrong-subject IDs, mixed proposals,
partial actions, current and future fields, stale answer release, rollback,
alternative-effect bypasses, and unresolved-hold retention.

The broader command was:

```sh
/home/thaddus/gatemem/.venv-nb/bin/python -m pytest -o addopts='' -q --tb=short \
  security_tests/test_recipient_revocation_v19.py \
  security_tests/test_personal_ownership_v19.py \
  security_tests/test_incremental_permissions.py \
  security_tests/test_selective_memory_v17.py \
  security_tests/test_compact_permissions_v18.py \
  security_tests/test_governance_and_erasure.py \
  security_tests/test_privacy_core.py \
  security_tests/test_two_store.py \
  security_tests/test_permission_inference.py \
  evaluation/gatemem_medical/tests
```

The final broad run returned **362 passed, 1 failed, 33 subtests passed**. The failure is
`test_generic_integration_can_explicitly_leave_ownership_disabled`, which also
failed on the verified baseline before implementation: the historical V19 prompt
unconditionally includes the personal OWNS instruction, while this inherited test
expects it omitted when ownership is disabled. That unrelated prompt-policy change
was not included. The accompanying validation summary records the final results
and implementation digest. No historical run was restarted and no paid evaluation
was launched.

## Historical evidence and remaining limits

The saved turn-138 proposal explicitly selected `scheduler_jules`, named two Jules
grants, and separately proposed a family restriction. This proves incorrect
recipient selection; it does not establish a `session.actor_id` fallback cause.
The exact source request is included in a hostile-recipient regression.

`analysis/v19-recipient-revocation/historical-t138-offline.json` records a read-only
check using all 138 retained source rows in that job's saved prefix. The new host
resolves **family_linda_park**, with the four source receipts for the corroborated
relationship. It rejects the saved two Jules revokes against reconstructed
prepublication assignment records. A relative's own kinship assertion alone
cannot attest the relationship for another subject. Later erasure also means
retained end-of-run rows are not a complete pre-turn snapshot. This verifies the
recipient invariant, not publication of the independent compound future-family
restriction or repair of the historical utility checkpoint. The later erasure
hold is unchanged. The analysis-only replay script beside the report reproduces
this check using immutable read-only SQLite connections.

Resolution deliberately supports a closed English grammar and immediate,
unconditional withdrawals. Unsupported names/relationships, relative dates,
collective “family” references without an explicit supported recipient set, and
mixed adverse controls retain protective behavior. Supported partial fields are
scheduling, billing, contact, clinical, budget, and pickup instructions. Their
conservative lexical recognition is not a general natural-language scope prover;
unrecognized facts and compound temporal facts can remain unavailable even when
some content is unaffected. Broader relationship/language/scope support needs
additional evidence contracts and tests in a later batch. No benchmark entity
roster, roles, expected answers, annotations, or case IDs supply runtime identity
or permission decisions.
