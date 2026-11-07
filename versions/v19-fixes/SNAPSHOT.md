# V19 source snapshot

This directory contains the code, tests, examples, and V19 design notes copied
from the `a-mem-v19-fixes` worktree, including its uncommitted fixes. It has no
nested Git repository and is independent of the root V21 implementation.
`SOURCE_MANIFEST.json` records the original commit, worktree status, file hashes,
and exclusions. This document and the reproduction environment template were
added after the source snapshot.

From the repository root, run `python scripts/verify_v19_bundle.py` to verify
the snapshot and exported results. Git attributes disable line-ending conversion,
and formatting hooks exclude the archived trees so their recorded hashes remain
valid.

All 78 files listed in the completed model-controls run's `source-hashes.json`
match this snapshot. Its exact frozen code and synthetic fixture data are also
included in the [evaluation exports](../../evaluations/v19/README.md).

## Use this version

Run commands from this directory so Python imports this version's
`agentic_memory` package, rather than the package at the repository root.
Install dependencies in your own virtual environment with `python -m pip install
-e .`. For tests, also install `pytest`.

```bash
cd versions/v19
python -m pytest evaluation/gatemem_medical/tests
# Optional broader historical/runtime regression suite:
python -m pytest security_tests evaluation/gatemem_medical/tests \
  --ignore=security_tests/test_answering_v20.py \
  --ignore=security_tests/test_workflow_v20.py
```

Some historical tests and analysis scripts refer to a separately installed
GateMem checkout or original machine paths. The evaluation command below uses
the bundled fixture data explicitly. The two V20 test files are retained as
historical source but import V20 symbols absent from this V19 worktree, so they
are excluded from the V19 validation command.

Bundle validation passed all 63 evaluation-harness tests and 27 subtests. The
broader suite was interrupted after 582 passing tests and 20 passing subtests
when progress stalled in the historical runtime's erasure-matching path; it is
not reported as a complete regression pass. Details are recorded in
`../../evaluations/v19/BUNDLE_VALIDATION.json`.

## Reproduce the completed run in a fresh output directory

Start a compatible OpenAI-style Qwen3.8-27B endpoint and configure its address
and your local embedding model path in `.env`. The example uses one endpoint
for permission inference, answering, verification, and benchmark judging, as
the completed run did. The endpoint's model alias must match the configuration.

```bash
cp evaluation/gatemem_medical/v19-local.env.example .env
# Edit .env for your model endpoint and optional local embedding path.
python -m evaluation.gatemem_medical \
  --env-file .env \
  --gatemem-repo ../../evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/frozen-dataset \
  --permission-mode inference --permission-version 19 \
  --answer-validation-mode enforce --preflight
python -m evaluation.gatemem_medical \
  --env-file .env \
  --gatemem-repo ../../evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/frozen-dataset \
  --permission-mode inference --permission-version 19 \
  --answer-validation-mode enforce --run-dir runs/v19-reproduction
```

Use a fresh output directory. The published result exports omit SQLite runtime
state and cannot be resumed directly. Different serving backends, precision,
and model generations may change the measured results; the archived migration
records describe the original mixed-serving run.

## Results and limitations

The full model-controls run scored all 579 checkpoints across 4,294 source turns:

| Metric | Result |
|---|---:|
| Utility | 160 / 210 (76.19%) |
| Answer-level privacy leakage | 12 / 192 (6.25%) |
| Answer-level deletion leakage | 2 / 177 (1.13%) |
| Privacy context exposure | 9 / 192 (4.69%) |
| Deletion context exposure | 0 / 177 (0%) |
| Utility over-refusal | 21 / 210 (10%) |

Evaluation coverage is complete. Processing is still marked `degraded: true`;
enrichment is not complete, and answer validation was unavailable on 77 queries
across all categories. These flags are retained in the original summary rather
than treating the run as an error-free execution. The input contract is
`identity-metadata-stream-v1`, not an unannotated raw-stream benchmark.

The benchmark judge is Qwen3.8-27B. The archived GPT-4.1 comparison report reviews
an earlier snapshot and does not establish a controlled same-output judge
comparison.
