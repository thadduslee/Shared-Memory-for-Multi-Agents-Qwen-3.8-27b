# Runs

One directory per domain run of this system. Each is laid out the same way; see the
docstring at the top of `pipeline/common.py`.

| Domain | Status |
|---|---|
| [`medical/`](medical/README.md) | Done 2026-11-09: 21 episodes, 579 checkpoints, both GPT-4.1 rejudges |
| `office/` | Not run yet: `bash pipeline/run_domain.sh office --rejudge` |
| `education/` | Not run yet: `bash pipeline/run_domain.sh education --rejudge` |
| `household/` | Not run yet: `bash pipeline/run_domain.sh household --rejudge` |

See `docs/RUNNING.md`. After a run, `python pipeline/export_run.py runs/<domain> runs/<domain>`
records the SHA-256 and size of the SQLite stores that git leaves out.
