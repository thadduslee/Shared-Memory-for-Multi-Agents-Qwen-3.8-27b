# Generated role policies

Every policy the system's own model has written, including failed and superseded attempts,
with full prompts and replies. New ones from `pipeline/build_policy.sh` land here as
`<domain>-v2-<UTC time>/` with a `.log` next to them. The archived files are from
`a-mem-variant/evaluations/v19-role-policies/`, and their hashes are in `EXPORT_MANIFEST.json`.

| Directory | Builder | Status |
|---|---|---|
| `medical-v2-20261108T165625Z/` | `build_role_policy.py` v2 (step 1 + 5 votes) | **Frozen and used** by the medical run: `runs/medical/role_policy.json`, sha256 `3de5a65c…` |
| `medical-v2-20261108T163002Z.log` | v2 | Interrupted during step 1; the directory was empty, so git does not keep it |
| `medical-inferred-20261108T145437Z/` | `infer_role_policy.py` (v1: one primary answer plus samples) | Superseded by v2; never used for a run |
| `office-inferred-20261108T141206Z/` | v1 | Superseded; its log ends in a connection error after the primary answer. Never used |
| `office-fixedkinds-diagnostic-20261108T141206Z.log` | v1 diagnostic with the hand-written office kinds fixed | Failed (model server down); the directory was empty |
| `current-office-kinds.json` | none | The hand-written office information types, kept as the input of that diagnostic |

There is **no v2 policy for office, education or household yet**. `pipeline/run_domain.sh`
generates one as its first step. Do not tune the builder's prompt or threshold on medical or
office scores (see `docs/HOSTS.md`).

## v2 format (`policy.json`)

```
format            "gatemem-role-policy-v2"
domain, model, prompt_version, script_sha256
information_kinds {kind: one-sentence description}        step 1, temperature 0, seed 0
role_descriptions {role: two or three sentences}           step 1; used as the role's care scope
votes, majority   5 and 3
role_kinds        {role: [kinds opened by >= majority]}    step 2, seeds 1..5, temperature 0.6
role_kind_votes   {role: {kind: votes}}
authorities, authority_votes                               office and education only
```

`provenance.json` holds the exact messages, JSON schemas, endpoint, input file hashes,
and every reply, with its reasoning, timing and token usage.
