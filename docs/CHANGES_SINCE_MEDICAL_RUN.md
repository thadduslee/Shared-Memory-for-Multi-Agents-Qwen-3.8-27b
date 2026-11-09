# Changes since the medical run

The medical run used the code whose hashes are in `runs/medical/frozen-source.sha256`.
These are all the changes made to those files afterwards. The full diff is in
[changes-since-medical-run.patch](changes-since-medical-run.patch).
`python scripts/verify_bundle.py` fails if any other frozen file differs.

**Effect on medical and office behaviour: none intended, and none found by the tests.** The
`MEDICAL` and `OFFICE` hosts are unchanged. The policy builder's medical and office prompts
are byte-identical to the ones that built the archived policies; the tests compare them
with the frozen medical provenance and with the original builder's office prompts. One
thing does change: the runner's `code_digest` covers the harness directory, so a fresh
medical run would record a new digest, and the old journals cannot be resumed with this code.

| File | Change |
|---|---|
| `evaluation/gatemem_medical/hosts.py` | Adds the `EDUCATION` and `HOUSEHOLD` hosts, which have no hand-written tables (see [HOSTS.md](HOSTS.md)). Adds `Host.requires_policy`, and `validate` refuses a host with empty tables unless `requires_policy` is set. Adds `MENTIONED_ROLES` and `policy_excluded_roles(domain)`, the roles the generated table leaves out |
| `evaluation/gatemem_medical/runner.py` | Pinned sizes for education (30/540/518) and household (23/552/545). `--gatemem-repo` defaults to `data/gatemem`. A `requires_policy` host without `--role-policy` is refused |
| `evaluation/gatemem_medical/rejudge.py` | `--gatemem-repo` defaults to `data/gatemem` |
| `evaluation/gatemem_medical/evolution_ablation.py`, `access_audit.py` | Data defaults point at `data/gatemem` |
| `scripts/build_role_policy.py` | Education and household entries. The prompt words for the organisation, its staff and the authority subject are now placeholders, and their defaults reproduce the old text exactly. `--gatemem-repo`, `--exclude-roles` and `--authorities` get defaults from `data/gatemem`, `hosts.policy_excluded_roles` and the domain table. The prompts are built by `step1_messages` and `step2_messages` so tests can rebuild them. The provenance file also records `authorities` and `wording` |
| `scripts/infer_role_policy.py` | Usage text only |
| `evaluation/gatemem_medical/tests/test_{scoring,office_host,input_boundary,role_policy}.py`, `security_tests/test_privacy_fallback_episodes_v19.py` | Test data paths point at `data/gatemem`, so they run instead of skipping on a clone |

New files (not in the medical run):

- `evaluation/gatemem_medical/tests/test_education_household_hosts.py`
- `evaluation/gatemem_medical/tests/test_policy_builder.py`
- `evaluation/gatemem_medical/tests/fixtures/office_prompts_original_builder.json`
- `pipeline/`, `scripts/verify_bundle.py`, `data/`, `runs/`, `references/`, `policies/`, `docs/` (except `docs/design/`)
