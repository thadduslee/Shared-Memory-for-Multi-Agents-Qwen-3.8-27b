# Policy model and decision precedence

## Inputs

The engine (`gsm.policy.engine.PolicyEngine`) is a pure function of:

* `PrincipalContext` (trusted): tenant, workspace, principal, roles, auth strength, purpose, attributes, request time;
* `MemoryFacts` (metadata only, **no content**): tenant/workspace, lifecycle state, retention, owners, subjects,
  sensitivity, compartments, parsed policy, policy revision, allowed views, derivation flags, resolved version;
* `AuthSnapshot` (loaded per request): principal active flag, active relationships, active grants (memory and
  compartment), active explicit denials;
* for derived memories, the live decisions on their parents.

## Per-memory policy (`MemoryPolicy`, strict JSON, unknown keys rejected)

| Field | Meaning |
|---|---|
| `rules[]` | `AccessRule`: who (`principal_ids` ∧ `any_roles` ∧ `relation`, or `tenant_wide`) gets which `view`, with optional `purposes`, `min_auth_strength`, `require_attributes` |
| `owner_view` | view owners receive; `null` removes owner privilege |
| `mandatory` | `purposes` / `min_auth_strength` that bind **every** allow path, owners included |
| `existence_visible_to[]` | predicates for whom a denied record may be acknowledged (REFUSE) |
| `historical_access` | `owners_only` (default) / `same_as_current` / `none` |
| `update_authority[]`, `delete_authority[]`, `grant_authority[]` | who besides owners may change it |
| `subject_can_delete` | a subject may request erasure |
| `strict_compartments` | compartments also bind owners and memory-level grantees |

`principal_ids` names specific principals (e.g. "share with Leo and Priya"); its SQL pre-filter tag is
`principal:<id>`.

Relations (`RelationRequirement`): `target="object"` (e.g. `member_of project:apollo`) or `target="subject"`
(e.g. `manager_of` one of the memory's subjects; the special relation `self` means the requester is a subject).

Organization-wide configuration (`EngineConfig`): role → compartment clearances (e.g. `hr` → `hr`,
`medical_leave`) and sensitivity → minimum auth strength (`confidential` → password, `restricted` → MFA).

Templates in `gsm.policy.templates`: `private`, `project`, `availability_record`, `hr_restricted`,
`tenant_public`. Model interpreters may only *name* a template.

## Read precedence

```
1.  tenant / workspace mismatch, snapshot mismatch, inactive principal  → DENY
2.  tombstoned or pending cascade invalidation                           → UNAVAILABLE
3.  retention expired; no version valid at the requested time            → UNAVAILABLE
    malformed stored policy                                              → DENY (fail closed)
4.  explicit denial on the memory, any of its compartments or subjects   → DENY (never acknowledged)
4b. mandatory constraints:
      purpose present but not permitted                                  → DENY
      purpose missing / auth strength too weak                           → ESCALATE, only if 5-7 would allow;
                                                                           otherwise DENY (no existence hint)
5.  owner (policy.owner_view)                                            ┐
6.  active explicit grant on the memory (expiry, revocation, purpose)    ├ allow paths: most revealing wins
7.  matching rule, with compartment clearance for every compartment      ┘
    (derived memories replace 5-7 with the minimum view over live parent decisions)
8.  otherwise                                                            → DENY
then: cap the view by allowed_views; for historical versions enforce historical_access
```

Owners do **not** automatically override organizational restrictions. Explicit denials and mandatory constraints
precede ownership. `owner_view: null` and `strict_compartments: true` remove owner privileges when needed.
Authorization is always evaluated *now*: querying `as_of` a past time never restores revoked access.

Decisions are `ALLOW` (full view), `ALLOW_REDACTED` (any lesser view), `DENY`, `UNAVAILABLE` or `ESCALATE`.
Each carries the permitted view, stable reason codes (`gsm.domain.reasons.Reason`; never renamed, only added),
`policy_version` (`gsm-policy/<engine>#r<policy revision>`), memory id, principal id and timestamp.
`existence_ack` is set only on DENY when `existence_visible_to` matches.

## Write authorization

`authorize_write(ctx, facts, snap, action)` first runs the read decision. If the requester cannot see the
memory at all, the result stays invisible, and callers map it to a uniform `NOT_FOUND`, identical to a missing
or cross-tenant id. Otherwise:

* **UPDATE / DELETE / GRANT:** owners (unless `owner_view` is null), then the matching `*_authority`
  predicates. DELETE also allows subjects when `subject_can_delete` is set.
* **DERIVE:** requires a FULL view.
* **Grants:** a non-owner can never grant a view above their own, and only views in `allowed_views`.
* **Restrictions (`RESTRICT_ACCESS`):** withholding a memory from named principals adds memory-level explicit
  denials, which outrank every rule and grant. It only narrows, so it is authorized like UPDATE. Lifting a
  restriction widens access again, so it is authorized like GRANT, and it lifts only restrictions created by
  this operation (`RESTRICTED_BY_PRINCIPAL`), never administrative denials. Owners cannot be restricted from
  their own memory. Model-proposed restrictions are quarantined like other high-impact operations.
* **Relationships:** `member_of` / `assigned_to` may be managed by `owner_of` the object; everything else
  (including `owner_of` itself) needs the `admin` role.
* **Re-classification:** anything not provably at least as restrictive (`is_at_least_as_restrictive`) needs
  `declassify=true` from an owner via the oracle path. Model proposals are quarantined.

## Adding a policy rule or condition

1. Extend the policy data model in `gsm/domain/policy.py` (keep `extra="forbid"`).
2. Evaluate it in the right precedence stage in `PolicyEngine`. Deny-type conditions go in stages 1-4b;
   allow-type conditions go in stages 5-7.
3. If it can *allow* access, make sure `gsm/policy/tags.py` still yields a superset for the SQL pre-filter.
   `tests/unit/test_schemas_logging_tags.py::test_access_tags_are_a_superset_of_rule_allows` enumerates rule
   shapes to check this.
4. Add a stable `Reason` code, and extend `is_at_least_as_restrictive` if the field can widen access.
5. Add unit tests in `tests/unit/test_policy_engine.py`.
