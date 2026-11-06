# Diagnosis of the first two V21 utility failures

The first two utility checkpoints are `ckpt_01` at turn 54 (Elena's medication
plan) and `ckpt_02` at turn 83 (Linda's permitted appointment logistics). Both
have now been scored as failures. The earlier second scored checkpoint,
`ckpt_11`, was a privacy checkpoint and passed. Checkpoint 2 completed during
this investigation; the initial evidence snapshot correctly records it as
pending at that earlier observation time.

The analysis reads the native run without changing its code, memory, permissions,
predictions or scores. Checkpoint 1's historical policy state is taken from the
pre-resume turn-54 database backup, avoiding attribution of later state to an
earlier answer. No external model calls were made for this investigation.

## Checkpoint 1: incorrect suspension plus answer omission

**Confirmed host recovery bug.** Elena's personal ownership grant was active
through version 4 and became suspended in version 5, published by recovery of
turn 7. That recovery proposed **zero permission operations**. Its original
input prefix contained turns 1–7, but the current ownership grant already had a
binding supported by turn 8. Turn 8 was still active and had not been deleted.

`workflow_v20.recover_one`, shared by V21, creates the recovery child with the
original job's inputs. `PermissionUpdater._validate` checks current assignments
using `_evidence`, which includes binding evidence, against that smaller input
set. It treats a source outside the historical prefix as missing/deleted and
suspends the grant. The stale-control check only examines proposed operations;
an empty plan can therefore still produce implicit damaging state changes.

The same mechanism suspended two of Elena's self-service grants during recovery
of turn 29 because their current bindings referenced live turn 30. Their policy
hashes did not change. The ownership compiler deliberately does not reactivate
suspended grants, so this state persisted.

This is independently reproducible with scripted processors and a temporary
store: [reproduce_recovery_prefix_bug.py](reproduce_recovery_prefix_bug.py),
with output in [reproduction.json](reproduction.json). A valid active ownership
grant becomes suspended after a no-operation historical recovery.

**Downstream exclusion.** Turns 50, 53 and 54 were extracted with Elena correctly
identified as the subject. They contain continuing-medication instructions,
nausea treatment, and the full medication recap. With ownership suspended, the
remaining patient grants were narrow service grants. Recorded negative reviews
include “Medication instruction is outside urine sample service scope” and
“Medication plan is not billing setup.” The turn-54 recap was materialized for
the nurse, but not for Elena. Completed negative reviews explain why zero
pending pairs did not mean the necessary evidence was available.

**Separate answer-selection failure.** The turn-24 prenatal-vitamin/levothyroxine
record was in the answer packet at index 7. The answer model labeled it
`unrelated` and did not cite it. The verified renderer preserved that selection,
so the continuing medication information was omitted. The judge marked only
three of seven required items covered.

The live hold at this checkpoint concerned `nurse_alvarez`, not Elena. It is not
the identified explanation for Elena's missing medication facts. Full policy,
review and source evidence is in [evidence.json](evidence.json).

## Checkpoint 2: confirmation context lost and wrong subject

Linda's logistics grant was active, with a read-access expiry on Friday March 6.
Its existence was not the missing-permission problem in these two omissions.

**Thursday detail lost across proposal and acceptance.** Turn 61 proposed
Thursday, March 5 at 8:10 AM. Its first segment was classified `context` and its
second segment `question`, so neither became a factual memory. Turn 62's
acceptance became only “Yes, Thursday at 8:10 works.” The answer included this
short confirmation, but lacked the full calendar date and AM designation. The
judge marked the complete Thursday requirement absent. The confirmed booking
needs grounded links to the earlier proposal's permitted logistical fields.

**March 11 booking attributed to its speaker.** Turn 68 said the scheduler had
booked a follow-up for Wednesday, March 11 at 3:10 PM. The extraction receipt
states `subject: scheduler_jules`, with reason “p11 states they booked the
appointment.” This describes Elena's appointment, but the model treated the
speaker as the information subject. Consequently no candidate/grant review
matched Linda's grant for Elena's information, and this appointment was absent
from the retrieved packet. The record was not merely ranked too low or clipped.

The final answer covered the Friday time, arrival and location but omitted the
complete Thursday and March 11 requirements: three of five items covered.
See [checkpoint-02-evidence.json](checkpoint-02-evidence.json).

The general pending queue contained 55 pairs. That count alone does not establish
the cause of these exact omissions; the extraction and subject receipts identify
the earlier failures. The packet also contains repeated copies of the same Friday
record, an additional retrieval-budget inefficiency rather than the identified
origin of the missing dates.

## Repair priorities

1. Separate original-event inference context from current live-evidence
   validation. A historical no-operation recovery must not suspend valid newer
   bindings; real deletion, revocation and expiry must remain enforceable.
2. Repair or reconstruct the earlier damaged policy state through reviewed
   replay or a fresh run. Correcting the code alone will not reactivate an
   already-suspended ownership grant.
3. Preserve grounded proposal/acceptance links and distinguish the subject of a
   service fact from its speaker, with independently validated correction paths.
4. Validate answer coverage of continuing relevant instructions. An `answer`
   action and complete citation schema do not establish a complete answer.
5. Always deduplicate the identical resource ID before allocating retrieval
   slots, while retaining scope/provenance boundaries for distinct resources.

These are diagnosed failures and proposed repairs; the running implementation
was not changed by this investigation.
