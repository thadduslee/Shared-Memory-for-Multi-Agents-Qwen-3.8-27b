# Domain hosts

A host gives the domain-neutral core the little it needs to know about a domain: who
records belong to, which roles do work for them, and who controls subjects that never
speak. All four are in `evaluation/gatemem_medical/hosts.py`. This system's role tables
come from a generated policy in every domain (`--role-policy`). The medical and office
hand-written tables stay in `hosts.py` only as the contaminated references the earlier
runs used.

## Rules for designing a host

These come from the leakage finding of 2026-11-08/09: both hand-written tables were written
with test-set visibility.

- A host may use the episode **entity schema** (principal roles, the kinds of `*_id` values)
  and the benchmark's **published policy paragraph**. It may not use checkpoints,
  expected actions or answers.
- Do not tune the policy prompt, the vote threshold or a host on medical or office scores.
  Develop on education or household, and report any change measured on only one domain as such.
- No role table is hand-written for a new domain. Set `requires_policy=True`, and the
  runner then refuses to run without a generated policy.

## Summary

| | Medical | Office | Education | Household |
|---|---|---|---|---|
| Subject roles | `patient` | `project` | `student`, `program`, `project` | `primary_resident`, `partner_spouse`, `adult_child`, `minor_child`, `elder_family_member`, `roommate` |
| Entity subjects (from `*_id`) | none | `project_*` | `program_*`, `project_*` | none |
| Only ever mentioned | `family_member` | none | `parent_guardian` | `guest` |
| Workers: the generated table's titles | 8 clinical and administrative roles | all 15 staff roles | 13 staff roles (`professor`, `advisor`, `ta`, `ra`, `registrar`, `financial_aid`, `department_admin`, `residence_staff`, `resident_assistant`, `dean_assistant`, `counselor`, `campus_it`, `club_officer`) | 9 helper roles (`building_staff`, `caregiver`, `cleaner`, `driver_pickup_contact`, `home_it_helper`, `household_manager`, `nanny`, `technician`, `trusted_contact`) |
| Authorities voted in the policy | no (patients control their own records) | yes, over projects | yes, over programs and projects | no |
| Conventions the model sees | core defaults (person subjects) | project wording | student + program/project wording | core defaults (person subjects) |
| Hand-written table in `hosts.py` | yes (reference only) | yes (reference only) | none, `requires_policy` | none, `requires_policy` |
| Dataset (episodes / checkpoints / boundaries) | 21 / 579 / 567 | 17 / 547 / 467 | 30 / 540 / 518 | 23 / 552 / 545 |

The builder leaves out the assistant (`agent`), the person subjects and the
mentioned-only roles (`hosts.policy_excluded_roles`). Every other principal role becomes a
row of the generated table; `test_every_principal_role_is_a_subject_a_worker_title_mentioned_or_the_assistant`
checks this for all four domains.

## Medical

Unchanged from the medical runs. A record belongs to a patient. Family and friends named
in a note are mentioned, not owners. Covering staff keep access for 24 hours after their
last work on the chart.

## Office

See [design/OFFICE_HOST.md](design/OFFICE_HOST.md). Projects are subjects, read from
`project_id` values only. Because projects never speak, someone must hold authority over
them. In this system that is the policy's authority vote, which replaces the hand-written
`('product_manager', 'manager', 'executive', 'security')`. Known limits: authority is per
role, not per project; and look-alike project comparisons are refused without a model call.

## Education

**Who owns a record.** The benchmark policy says: "Records may belong to a student,
research project, campus program, …". Students own their records, like patients: a student
reads their own, and nobody else gets them by default. Campus programs (`program_*`, one
per episode) and the research project (`project_atlas`) are entity subjects with
authorities, as office projects are.

**Mentioned, not owners.** Parents and guardians do no work for a student, so they are
neither subjects nor workers; this matches the policy's "Parent/guardian access is limited".

**Not entities.** Case labels (`case_id` such as `aster_waiver`) and the single
`building_id` and `course_id` carry no kind prefix, and only the relationship rows (which
stay behind the input boundary) give them meaning.

**Names.** `program_atlas_scholars` becomes "Program Atlas Scholars", alias "Atlas
Scholars". `project_atlas` becomes "Project Atlas", alias "Atlas". Aliases match longest
first, so "Atlas Scholars" is always the program. Every program and project name occurs
in its episode's text.

**Conventions.** The task and identity wording says that student records belong to the
student, and that program and project records belong to the program or project. It also
says that `bootstrap.authorities` lists who controls programs and projects, and that a
configured authority's removal request is a deletion request for that entity.

**Known limits and things to watch in the first run:**

- A student is not a worker. A participant asking about a program they belong to gets
  program facts only through an explicit grant from a program authority. Utility on public
  program logistics may suffer.
- Authority is per role, not per program: the directory has no membership.
- If the policy's authority vote names nobody, `freeze_policy.py` stops, because nobody
  could restrict or delete program records. `--allow-no-authorities` overrides this.
- Stage 2 refuses, without a model call, a question naming two look-alike subjects. "Is
  Atlas the same as Atlas Scholars?" is refused.

## Household

**Who owns a record.** The benchmark policy says: "a resident may access current household
information they directly own or manage". Every role that lives in the home is a subject
role. So a guest named beside the owner in an access plan does not block a helper, and a
helper's statement about a resident is about that resident.

**Mentioned, not owners.** Guests do no work for a resident.

**Workers.** Helpers and service providers, limited to their generated row. The policy
paragraph lists partners and roommates among the scoped readers. Here they are residents,
which means subjects, not workers. If they were workers, their statements about themselves
would be read as work for someone else, and the core would leave those facts unattributed.

**No household entity.** `household_id` values such as `oak_house` are not subjects. A
house subject would need an authority role, and the authorities over a home are its
residents, who are subjects, not workers (`Host.validate` requires authority roles to be
workers). So shared home facts, such as a door code, belong to the resident they are
attributed to.

**Known limits and things to watch in the first run:**

- **Other residents need the owner's grant.** A partner asking for the door code that the
  primary resident gave gets nothing without an explicit grant.
  `test_another_resident_needs_the_owners_grant` pins this behaviour. If household utility
  is low for that reason, the next design step is a household entity whose authorities are
  residents, which needs a core change to `Host.validate` and `subject_authorities`.
- A care label counts only for a helper who speaks or is named in the turn (`care_v19.labels`).
  "The cleaner arrives Tuesday" with no name gives the cleaner nothing.

## Tests

```bash
python -m pytest -o addopts= -q evaluation/gatemem_medical/tests/test_education_household_hosts.py \
  evaluation/gatemem_medical/tests/test_office_host.py evaluation/gatemem_medical/tests/test_role_policy.py
```

The education and household tests use small fixture policies and scripted labels. They
cover each staff role reading only its kinds for the student or resident it works for;
students and residents reading their own records; parents and guardians reading nothing;
program records reaching only staff working on that program; a program authority being
able to delete while other staff cannot; and helpers named beside a guest. Only an episode
run shows how the real model labels facts and how good a generated policy is.
