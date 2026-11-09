# Where everything came from

Before this repository existed, the work was spread over three places on the lab machine.
This page maps each of them to its place here. The originals were only read, never
changed, and are all still on the machine.

## The three sources

| Source | What it was | Where it is here |
|---|---|---|
| `/home/thaddus/a-mem-v19-fixes` | Git worktree of `a-mem-variant`, branch `v19-recipient-revocation` at `02a7c8f` (2026-11-03) plus 39 uncommitted files: the V19 runtime. **That branch is not on GitHub.** | Nowhere directly. It was snapshotted into `a-mem-variant/versions/v19`; apart from `analysis/` data the two are identical (checked with `diff -r`). Its code is the ancestor of everything here |
| `/home/thaddus/a-mem-variant` | Branch `directed_edges_variant` (on GitHub). `versions/v19` is the V19 baseline snapshot; `versions/v19-fixes` adds Stages 1–2, the erasure fix (commit `072c244`, 2026-11-07), request detection (commit `8632de9`, 2026-11-08), and the uncommitted learned access, role-policy loading, office host, policy builder and tests. `evaluations/v19-role-policies/` holds the generated policies; `evaluations/v19-rejudges/` holds the rejudges | The code at the repository root (from the frozen run copy, below), the design notes in `docs/design/`, the figures in `docs/figures/`, the tests, `policies/`, and the medical baseline's rejudge reports in `references/medical_domain_original/rejudges/` |
| `/home/thaddus/v19-evaluations/medical_domain_new_system` | The medical run of this system (2026-11-09): `frozen-source/` (exact code), the frozen policy, 21 episode results, smoke test, both GPT-4.1 rejudges, and the as-run scripts | `runs/medical/` (everything except SQLite, locks, credentials and `frozen-source/`), with the code at the repository root |

Also used, read-only:

| Source | Used for | Here |
|---|---|---|
| `/home/thaddus/GateMem` @ `d3b73fe` | Benchmark data, `domains.py`, scorer | `data/gatemem/` |
| `/home/thaddus/a-mem-gov` @ `a297102` | Strict govmem judge and prompts | `data/govmem-strict-judge/` |
| `/home/thaddus/v19-evaluations/medical_domain_original` | Same core with the hand-written medical table (the comparison baseline) | `references/medical_domain_original/` (scores and summaries) |
| `/home/thaddus/v19-evaluations/office_domain` | Office run of the same core with the hand-written office table, plus its two GPT-4.1 rejudges | `references/office_domain/`, and the strict-judge copy, which was checked against `a297102` |

## How the code here relates to the medical run

The code at the repository root started as a byte copy of
`medical_domain_new_system/frozen-source`. That is `072c244`'s core plus the uncommitted
learned access (`care_v19.py`, `permissions.py`), the `--role-policy` harness and the office
host. The run's own `base-commit.txt` says: "The request-detection patch in 8632de9 is NOT
included". Its 120 file hashes are in `runs/medical/frozen-source.sha256`.

Twelve files changed after the run. Each is listed with its reason in
[CHANGES_SINCE_MEDICAL_RUN.md](CHANGES_SINCE_MEDICAL_RUN.md), and the full diff is in
[changes-since-medical-run.patch](changes-since-medical-run.patch).
`python scripts/verify_bundle.py` checks that the other 108 files are still byte-identical,
and that no unlisted change crept in. Two documents moved unchanged: `FIXES.md` and
`OFFICE_HOST.md` are now in `docs/design/`.

**Not included: request detection (`8632de9`).** It added an `asks_for_information` field so
that requests are not stored as facts. In the pilot it moved 7 checkpoints from pass to
fail and none from fail to pass, and it caused a new privacy leak. It was deliberately
left out of this system. It remains on `origin/directed_edges_variant`, and
`docs/design/FIXES.md` describes it.

## Earlier runs not copied here

These used earlier systems and stay where they are:

- `a-mem-variant/evaluations/v19/`: the V19 baseline exports (the model-controls full run has U 0.762).
- `v19-evaluations/v19-*`: V19 pilots, fixes and partial runs.
- `a-mem-variant/evaluations/v19-rejudges/v19-{original,gov-strict}-*`: rejudges of the original V19.
- The full SQLite stores of every run, including the 2.3 GB of
  `medical_domain_new_system/results/*/memory*.sqlite`.

## Timeline

| Date (+08) | Event |
|---|---|
| 2026-11-03 | V19 runtime on `v19-recipient-revocation` (`a-mem-v19-fixes`) |
| 2026-11-06 | V19 snapshotted into `a-mem-variant/versions/v19` |
| 2026-11-07 | Stages 1–2 and the erasure fix (`072c244`); full medical run with the hand-written table → `medical_domain_original` |
| 2026-11-08 | Office host and office run with the hand-written table → `office_domain`; request detection committed (`8632de9`) and set aside; both hand tables found to be written with test-set visibility; policy builder v2 |
| 2026-11-09 01:19 | Medical policy generated and frozen (`3de5a65c…`); 21 episodes launched |
| 2026-11-09 12:47 | All 21 episodes finished; GPT-4.1 original-judge and strict rejudges follow |
| 2026-11-09 | Everything consolidated into this repository; education and household hosts and the domain pipeline added |
