# V19 Batch 6: task contracts and trusted workflow provisioning

Workspace: `/home/thaddus/a-mem-v19-fixes`, branch `v19-recipient-revocation`.
Batch base: `e4a095f`. Earlier fixes, including Batch 5, remain intact. This batch
does not modify the original checkout, historical runs or V20/V21 workflows.

## Stage A: structured receipts and dependencies

`task_contracts_v19.py` adds a host-configured `DependencyRegistry`, immutable
policy pinning, versioned private task receipts, dialogue admission, and runtime
fact gates. A dependency graph is a necessity specification, never authority.
Only an authenticated subject acceptance under a configured dialogue convention
can create a dialogue receipt. Unsupported or ambiguous mappings fail explicitly.

Every receipt records ID/version, tenant/namespace, subject, worker, requester,
beneficiary and authority; original service objective/task category; prerequisite
inputs, outputs, instructions, updates/cancellations and follow-through; actions;
item constraints, closed categories and exclusions; activation/expiry/state;
original offer/acceptance evidence; dependency policy ID/version/content digest;
and a subject/action/category/lifetime authority envelope.

The host expands and validates transitive dependencies, rejecting unknown nodes,
cycles and out-of-envelope prerequisites. Explicit closed field limits and
exclusions prune effective categories, including dependants of denied inputs.
Generated topic hints do not define a contract. Optional `generated_topic_hints`
are retained with `topic_hints_are_authority: false`; explicit closed limits come
from the authenticated acceptance mapping or verified signed payload. Trusted configured patterns must
prove membership using the **whole fact**, with escaped literal task items.
Multiple matching categories deny. Compound or unrecognized facts remain denied.
This intentionally conservative implementation cannot expose a complete chart or
document just because a sentence includes an allowed topic.

The existing independent semantic candidate/assignment review, source liveness,
attribution, restrictions, recipient revocation, erasure and scoped holds still
apply. Contract admission initially creates an **unbound** grant. A task ending
disables only its own grant; independent grants retain their own checks. A model
operation cannot strip or rewrite a trusted assignment's contract.

### Registry configuration and dialogue interface

See `examples/v19-task-policies/{adherence,parcel,repair}.json`. These are separate
illustrative domain policies, not automatically enabled benchmark policy. They
use the same core mechanism. Task actions currently support read/search/derive.
Trusted regexes must consume complete inputs; review them as authorization code.
`{items}` in category patterns becomes an escaped literal alternation, never a
regular expression supplied by a worker. Offers capture only a named `items`
group, split by the configured delimiter. Accepted response alternatives encode
closed-category limits and exclusions separately. Terminal patterns require an
exact task ID and authenticated subject restriction authority (or worker
completion). Unsupported natural language retains the existing protected path.

```python
registry = DependencyRegistry(json.load(open("policy.json")))
updater = PermissionUpdater(core, host_ctx, compartment, bootstrap, processor,
                            version="19", task_registry=registry)
# Both sources were authenticated by normal ingestion, and control processing
# completed. Admit at the acceptance source boundary, before later sources.
receipt = updater.accept_dialogue_task(offer_source_id, acceptance_source_id,
                                      task_id="stable-business-task-id")
# Normal source processing or explicit bounded flush_bindings reviews facts.
report = updater.task_contract_report()
```

Admission is an explicit host interface, not a model operation or an unrestricted
English intent parser. Compatible multiple offers cannot be selected by recency,
role or an arbitrary caller pointer. The original Batch 2 acceptance/repair
ledger continues unchanged. These contracts add a separately configured trusted
dialogue policy; they do not reinterpret old accepted-service labels silently.

### Migration

Disabled by default. V19 creates additive `privacy.task_policies_v19` and
`privacy.task_receipts_v19` tables. A registry (including disabled state) is pinned
per compartment; restart must supply identical configuration. Enable or change
policy using a fresh compartment/store with a reviewed forward ingestion plan.
Do not backfill authority from old labels, roles, roster entities, checkpoints or
gold answers. Preserve both SQLite files, receipt history and policy pins.
Older readers do not enforce receipt links: do not downgrade a populated store.
Source semantic budgets/attempts and existing prompt identities are unchanged.
Admission and graph checks make zero model calls; bindings use the existing
bounded scheduling and cache. No extra source-processing call is introduced.

Stage A tests and results are recorded under `analysis/v19-task-contracts/`.
Real-model validation is still required; offline fixtures do not establish
natural-language extraction quality or changed benchmark scores.

## Stage B: authenticated host-event JIT integration

`host_tasks_v19.HostTaskGateway` is a separate opt-in authentication adapter.
Construct it only inside the policy host, with explicit issuer policies and
host-held keys. The gateway is never registered as a semantic tool. Host session
authentication, signature verification, and issuer authority validation are all
required. Worker-authored notes, job titles, `workflow_evidence` text and claims
of being assigned cannot invoke it. The current implementation authenticates
events with HMAC-SHA256; each key must contain at least 32 bytes. Keys are never
stored in receipts, database policy or model prompts. Persisted key fingerprints
pin restart configuration. Trusted signer/host isolation and key distribution
are responsibilities of the deployment's authentication adapter.

Examples: `examples/v19-task-policies/host-issuers.json` and
`examples/v19-task-policies/host-assignment.json`. Replace illustrative identities,
namespace and timestamps with actual upstream authenticated values. Load key
bytes from a host secret store; no example secret is included.

```python
from agentic_memory.privacy.host_tasks_v19 import HostTaskGateway
gateway = HostTaskGateway(updater, issuer_policies,
                          verification_keys={"dispatch-2026-01": secret_key_bytes})
result = gateway.apply(host_ctx, event, signature_hex)
```

The external issuer signs UTF-8 `system.encoded(event)` (canonical sorted compact
JSON) using HMAC-SHA256; pass its hexadecimal digest separately. The signed event
has exactly `event_id`, stable logical `task_id`, monotonic integer `revision`,
`operation`, `issuer`, `key_id`, `namespace`, `tenant`, `authority`, `occurred_at`,
and `payload`. Unsupported fields/operations, unknown keys/issuers, wrong hosts,
authority/tenant/namespace mismatches, future events and missing trusted clocks
fail explicitly. A valid signature alone cannot exceed issuer policy.

`assign`, `reassign` and `handoff` require the full assignment payload in the
example: subject, worker, requester, beneficiary, supported task category, exact
items, activation/expiry, closed categories, exclusions, recipient limits and
authority envelope. The envelope names subject, worker, actions, categories and
lifetime. The issuer policy separately bounds subjects, workers, task categories,
actions, dependency categories and maximum lifetime. Both task policy and issuer
policy must authorize every transitive prerequisite. Requester/beneficiary
identity does not itself receive a read grant.

`complete`, `expire`, `revoke` and `cancel` require `{}` payloads and the next
revision of an existing stream owned by that issuer. Expiry events cannot fire
early. Runtime gates deny at expiry even before a lifecycle event or another
source arrives. Later source processing records expiry locally and idempotently.
Ended tasks cannot be reassigned, handed off or reactivated. A new independent
task needs its own authenticated assignment; prior recipient withdrawals still
apply. Ordinary source revocation, restrictions, erasure and active holds also
remain effective against every new task grant. The host receipt cannot resolve a
hold or reverse a subject withdrawal. Receipt-backed assignments inherit existing
recipient withdrawal evidence rather than restoring access through a new ID.

Logical task IDs remain stable across the stream. Each assignment revision gets
a stable receipt ID `<task_id>:r<revision>` and its own recipient-specific grant;
each receipt retains versioned lifecycle history. Handoff/reassignment atomically
ends the old receipt and creates a new **unbound** recipient grant. It cannot
expand subject, original items, actions, categories, lifetime, requester or
beneficiary, remove exclusions, widen closed limits or bypass original recipient
limits. A wider service requires separate original authority. Individual facts
are reviewed for the new recipient; bindings are never transferred wholesale.
Completion/revocation affect only that task's grant. Other independent grants
retain their own access and all existing checks.

`event_id` is the retry idempotency key. Exact retries return the saved result
without writes or model calls, including retries after later terminal events.
Changed replay bytes and missing/out-of-order/stale revisions fail. Source
controls must complete (or reach the existing protected terminal state) before
admitting a new host event. Event evidence, receipt transitions, grant publication
and stream position commit atomically. Failed publication rolls everything back.

Private event evidence uses resource kind `host_task_evidence`. It joins
authorization evidence for binding reviews; it never becomes a raw dialogue turn,
candidate fact or control segment. Verified identities may join the host bootstrap
without ownership, role grants or new source-consent authority. The same ordinary
binding queue handles JIT assignments; admission itself makes no model call.
Unchanged contract/policy/evidence retains binding fingerprints and cached
decisions. New recipients require their own review. Source repair remains capped
at two responses, and the existing adapter still defers maintenance after that
budget has been spent.

### Host integration migration and evaluation boundary

Stage B adds private `host_task_policies_v19`, `host_task_events_v19` and
`host_task_streams_v19` tables, created only when the gateway is configured.
Issuer policies and key fingerprints are pinned per compartment; key rotation or
policy changes require an explicit migration to a fresh compartment with reviewed
upstream event resumption. Restore stream positions, event IDs, receipt versions,
key configuration and both SQLite stores together. Do not reset semantic attempts
or downgrade to readers that ignore receipt constraints.

The GateMem raw-stream adapter does **not** construct a gateway, configure a
registry, or manufacture events. Its V19 processing metrics now report the
dependency policy, dialogue/host receipt counts, configured host integration,
actual host-event availability, raw-stream input contract and outstanding
real-model validation. Having the optional gateway configured is distinguishable
from having authenticated events. Reporting grants no access.

The supplied cards `001-07` and `006-04` are boundary replay inputs only. The
replay script reads `selected_source_turns`, strips roles/other annotations using
the existing input allowlist, and uses a no-op offline processor in fresh stores.
It reports four and two source calls respectively, zero fabricated host events,
and disabled host integration. It does not claim full-prefix semantic replay,
recovered acceptance or improved historical scores. In particular Omar's
authored recap remains insufficient assignment evidence. The accepted adherence
fixtures demonstrate task-specific prerequisites; they do not justify releasing
an entire mixed historical medication-list candidate. The original accepted
service and a reviewed deployment policy must establish the necessary regimen.

Validation results and exact commands are recorded in
`analysis/v19-task-contracts/validation.json`. Real-model validation of contract
binding accuracy, coverage and latency is still needed.

## Review revisions and results

Stage A checkpoint: `4f11fe4` (22 new tests at that checkpoint). Stage B is the
subsequent commit containing the host-event gateway and the final validation
report. Review Stage A with `git diff e4a095f 4f11fe4`, Stage B with
`git diff 4f11fe4 HEAD`, and all of Batch 6 with `git diff e4a095f HEAD`.

Final Batch 6 suite: **59 passed**. Broader offline compatibility suite:
**552 passed, 33 subtests passed, one known baseline test deselected**. The
excluded test is
`test_generic_integration_can_explicitly_leave_ownership_disabled`; its unchanged
V19 ownership-prompt expectation is documented in the earlier batches. The final
batch-only run includes the additional cross-registry-rule ambiguity test added
after the broader suite collected tests. Card boundary replay, compilation and
`git diff --check` also passed. No real model or historical benchmark rerun was
performed. See the JSON report and adjacent logs for exact commands and scope.
