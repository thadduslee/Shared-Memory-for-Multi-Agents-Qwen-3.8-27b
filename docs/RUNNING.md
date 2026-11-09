# Running a domain

Every command below runs from the repository root. The pipeline repeats what the medical
run did (`runs/medical/autopilot.sh`), with nothing domain-specific left in the scripts.
Each script's header comment documents its options.

## 1. One-time setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest httpx
cp pipeline/config/model.env.example pipeline/config/model.env          # local vLLM (as in the medical run)
cp pipeline/config/openrouter.env.example pipeline/config/openrouter.env  # only for rejudges; add the key
cp pipeline/config/soclaas.env.example pipeline/config/soclaas.env      # only if some episodes go to SOCLAAS
python scripts/verify_bundle.py
python -m pytest -o addopts= -q evaluation/gatemem_medical/tests pipeline/tests
```

The pipeline uses `.venv/bin/python` if it exists, otherwise `$AMEM_PYTHON`, otherwise
`python3`. On the lab machine, `AMEM_PYTHON=/home/thaddus/gatemem/.venv-nb/bin/python` is
the environment the medical run used.

`model.env` needs the local all-MiniLM-L6-v2 snapshot in `AMEM_EMBEDDING_PATH`. The medical
run used revision `1110a243…`. Every episode's `manifest.json` records the embedding
fingerprint.

## 2. Serve the model

```bash
tmux new -s epm
bash pipeline/serve_vllm.sh        # GPUS=0,3 by default; MODEL_PATH, VLLM, PORT overridable
```

This is Qwen3.8-27B in bfloat16 with tensor parallel over the given GPUs, 262k context,
4 concurrent sequences, the guidance structured-output backend and `qwen3.8:27b` as the
served name: the medical run's exact flags. **On the lab machine, GPUs 1, 2, 4 and 5 run
another user's (`minhnh`) server; leave them alone.**

## 3. Run the whole evaluation

In a second tmux window:

```bash
bash pipeline/run_domain.sh office --rejudge
bash pipeline/run_domain.sh education --rejudge
bash pipeline/run_domain.sh household --rejudge
```

`run_domain.sh` does the following, logging to `runs/<domain>/autopilot.log`. Any failed check stops it before launch.

1. **Policy.** Runs `pipeline/build_policy.sh <domain>` against the model in `model.env`:
   step 1 plus five votes, written to `policies/<domain>-v2-<time>/`. For medical this took
   23 minutes (step 1 plus five votes in parallel). To reuse an existing policy instead, pass `--policy policies/<dir>`.
2. **Freeze.** `pipeline/freeze_policy.py` checks the policy: its roles equal the domain's
   job titles, every role reads something, the host accepts it, and entity hosts have
   authorities. It then writes `role_policy.json` (with `.sha256` and `.provenance.json`)
   and `policy-comparison.md`. **Read `policy-comparison.md` before trusting a run.**
3. **Smoke test.** `pipeline/smoke_test.sh` makes an offline run of the first episode
   (two boundaries, scripted processors) and checks that the manifest names this exact policy.
4. **Launch.** `pipeline/launch.sh` opens one tmux window per episode in session
   `epm-<domain>`. Each window runs `pipeline/run_episode.sh` with the medical run's harness
   flags: `--permission-version 19 --answer-validation-mode enforce --role-policy …
   --learned-access-sources 2 --reply-access`. A failed episode resumes from its own journal,
   up to 5 attempts.
5. **Summary.** When every episode has an exit code, `pipeline/compile_summary.py` writes
   `runs/<domain>/summary.json`. For office it includes per-checkpoint comparisons against
   `references/office_domain`, the hand-written-table run.
6. **Rejudges** (only with `--rejudge`). `pipeline/rejudge_gpt41.sh` runs the benchmark's own
   judge with GPT-4.1. `pipeline/rejudge_strict.py` runs the strict govmem judge with GPT-4.1.
   Both go through OpenRouter and need no GPU.

To review the policy before spending GPU hours on the episodes, use `--no-launch`, which
stops after the smoke test. Then rerun with `--policy policies/<that dir>` and a fresh
`--run-dir`, or delete the run directory first.

### Options

| Variable or flag | Default | Effect |
|---|---|---|
| `--run-dir DIR` | `runs/<domain>` | Where results go; refused if `results/` is not empty |
| `--policy DIR` | generate a new one | Use a policy built earlier |
| `--rejudge` | off | Also run both GPT-4.1 rejudges at the end |
| `--no-launch` | off | Stop after the smoke test |
| `SOCLAAS_EPISODES="_001_ _002_"` | none | Send these episodes to the SOCLAAS endpoint (`soclaas.env`) |
| `MAX_PARALLEL=N` | 0 (all at once) | At most N episodes run at once. Medical ran all 21 at once on one server (`--max-num-seqs 4`) and took 11.5 hours |
| `SESSION=name` | `epm-<domain>` | tmux session for the episode windows |

The two SOCLAAS-vs-local medical runs showed a large backend effect (MGS 0.658 against
0.833). For comparisons between systems, keep the backend split the same, or run every
episode locally.

## 4. Watch it

```bash
python pipeline/status.py runs/office          # per-episode turns, checkpoints, utility, exit codes; pooled scores
tail -f runs/office/autopilot.log
tmux attach -t epm-office                      # one window per episode
```

## 5. If something fails

- **One episode exits non-zero after 5 attempts.** Fix the cause, then run
  `bash pipeline/run_episode.sh runs/<domain> local _NNN_`. It resumes from the episode's
  journal. When every exit code is 0, run `python pipeline/compile_summary.py runs/<domain>`.
- **The autopilot died after the launch.** The episodes keep running in their own windows.
  Run `compile_summary.py` yourself once they finish.
- **A rejudge stopped part-way.** Rerun it. `rejudge_gpt41.sh` resumes from
  `gpt-4.1-rejudge/rejudge.sqlite`, and `rejudge_strict.py` reuses every cached reply.

## 6. Run pieces by hand

```bash
bash pipeline/build_policy.sh education
python pipeline/freeze_policy.py education policies/education-v2-<time> runs/education
bash pipeline/smoke_test.sh runs/education
bash pipeline/launch.sh runs/education
python pipeline/compile_summary.py runs/education                 # --baseline DIR to compare with another run
bash pipeline/rejudge_gpt41.sh runs/education                     # --preflight checks the key with one tiny call
python pipeline/rejudge_strict.py runs/education
```

## 7. Commit the results

The ignore rules keep the SQLite stores, judge journals, locks and `*.env` files out of
git automatically. Before committing, record what was left out:

```bash
python pipeline/export_run.py runs/office runs/office        # writes runs/office/EXPORT_MANIFEST.json
git add runs/office policies && git status --short | head
```

The manifest lists every tracked file's SHA-256, and every left-out file's SHA-256 and
size, except credentials. Keep the run directory on the machine if you may ever need to
resume or audit the stores.
