# Office host settings

This file describes how the GateMem office domain is configured for V19 with Stages 1 and 2
and the erasure fix. The core in `agentic_memory/privacy` is unchanged. The office settings
are host configuration in `evaluation/gatemem_medical/hosts.py`. The medical settings moved
there unchanged, and a medical bootstrap is byte-identical to the one built before the move,
for all 21 medical episodes.

## Run it

```bash
python -m evaluation.gatemem_medical --domain office --gatemem-repo /home/thaddus/GateMem \
  --env-file <env> --permission-mode inference --permission-version 19 \
  --answer-validation-mode enforce --run-dir <dir>
```

`--domain` picks `bench/data/<domain>` and the host settings, and the dataset is pinned
(office: 17 episodes, 547 checkpoints, 467 boundaries). `rejudge.py` also takes `--domain`.
The metadata-assisted baseline (`--permission-mode baseline`) is medical only.

## Entities

| Entity | Where it comes from | Role in the directory |
|---|---|---|
| Staff | `entities.principals`: ID, display name, role | Their office role, for example `security` |
| Projects | Distinct `project_id` values in `entities.relationships`, and nothing else from those rows | `project` |

`project_maple` becomes "Project Maple", with aliases "Project Maple" and "Maple". Aliases
match case-sensitively and as whole words, so "Maple" never matches inside "Maplemark".
Every project short name occurs in its episode's text. An entity ID that collides with a
person ID is refused.

Project membership, `contractor_for_project`, `executive_delegate`, `access_scope` and the
other relationship fields still do not cross the input boundary.

## Who a record belongs to

`subject_roles = ('project',)`. A record about a project belongs to that project. Staff who
speak about it or are named in it are mentioned, not owners. Every person in the office
directory is a worker role, so their own statements about project work are not about them.

## Information types

The model tags each fact with one or more of these.

| Type | Covers |
|---|---|
| `schedule` | Target and launch dates, milestones, timelines, meetings, reviews, calendar holds |
| `status` | Progress, open work and blockers, readiness, approved broad descriptions such as a generic customer phrase |
| `customer` | Which external customer or account a project is for, customer mappings and contacts |
| `financial` | Budgets, costs, pricing, discounts, commercial caps, financial approvals |
| `contract` | Contract terms, redlines, renewal structure, legal review positions |
| `vendor` | Vendor and supplier options, evaluations, selection, procurement, onboarding |
| `incident` | Incidents, diagnoses, root causes, bridge or coordination labels, logs, repro notes, remediation |
| `credential` | Tokens, keys, passwords, secrets, privileged configuration values |
| `access` | Who has or lacks which access, its scope and duration, grants and revocations |
| `personnel` | HR matters about individual staff: staffing, performance, leave, compensation, hiring |

## What each role may read

A role reads these types on a project only after a source turn shows that person working on
that project (`care_participants`). A fact is readable only when all of its types are in the
reader's row. The model never sees this table.

| Role | schedule | status | customer | financial | contract | vendor | incident | credential | access | personnel |
|---|---|---|---|---|---|---|---|---|---|---|
| product_manager | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ | |
| manager | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | ✓ | ✓ |
| executive | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ | |
| engineer | ✓ | ✓ | | | | ✓ | ✓ | ✓ | ✓ | |
| sre | ✓ | ✓ | | | | | ✓ | ✓ | ✓ | |
| security | ✓ | ✓ | | | | ✓ | ✓ | ✓ | ✓ | |
| finance | ✓ | ✓ | | ✓ | ✓ | ✓ | | | | |
| legal | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | | |
| sales | ✓ | ✓ | ✓ | ✓ | ✓ | | | | | |
| procurement | ✓ | ✓ | | ✓ | ✓ | ✓ | | | | |
| operations | ✓ | ✓ | | | | ✓ | ✓ | | | |
| executive_assistant | ✓ | ✓ | | | | | | | | |
| employee | ✓ | ✓ | | | | | | | | |
| contractor | | ✓ | | | | | ✓ | | | |
| hr | | | | | | | | | | ✓ |

No role reads both `credential` and `customer`.

## Project authority

A patient controls their own records. A project never speaks, so the host says who controls
its records: `subject_authority_roles = ('product_manager', 'manager', 'executive', 'security')`.
Staff in these roles may grant access to a project's information, restrict it, and ask for it
to be deleted. Without this, the core refuses every deletion of project information, because
erasure needs the issuer to hold `restrict` authority over the record's subject.
`test_without_project_authorities_even_security_cannot_delete_project_information` shows this.

These staff are known participants from the first turn, so their first statement can act.

## Other settings

| Setting | Office value | Note |
|---|---|---|
| `temporary_care_hours` | 24 | Same judgment call as medical |
| `task_convention`, `identity_convention` | Office wording in `hosts.py` | Say that projects are subjects, that `bootstrap.authorities` lists who controls them, and that an authority's request to remove project information is a deletion request |

## Known limits

- **Authority is per role, not per project.** The directory has no membership, so the lead of
  one project holds authority over its sibling project too.
- **SRE cannot delete.** One office turn (episode 002) has an SRE asking to delete a bridge
  label; under this setting it is not a deletion.
- **Product managers cannot read credentials.** Two utility checkpoints ask a product manager
  for the active staging token.
- **Core prompt wording.** The attribution prompt still says that only the person whose
  information it is can request deletion, and still has medical examples. The office identity
  convention tells the model that project authorities can, but the core prompt is unchanged.
- **Cross-project questions.** Stage 2 refuses, without a model call, a question naming two
  look-alike subjects. Projects are subjects, so "are Maple and Maplemark the same account?"
  is refused.
- The tests use scripted labels. Only an episode run shows how the real model labels office facts.

## Tests

```bash
python -m pytest evaluation/gatemem_medical/tests/test_office_host.py
```

They cover the host tables, the input boundary, project authorities, the adapter bootstrap
for both domains, an offline office ingest, and these core behaviours under the office
settings: each role reads only its types, the sibling project's lead reads nothing of this
one, staff never shown working read nothing, a project authority can delete a value, and a
non-authority cannot.
