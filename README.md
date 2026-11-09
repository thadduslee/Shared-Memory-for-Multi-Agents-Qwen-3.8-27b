# EvolvingPrivacyMem

A privacy-governed long-term memory for multi-agent LLM systems. It is built on A-MEM, a
Zettelkasten-style agentic memory, and adds a two-store core that decides at ingestion time
who may read each remembered fact. The decision comes from the conversation itself plus a
small host configuration, and it is enforced again on every read, revocation and deletion.

This repository holds one system, its full medical evaluation on the GateMem benchmark,
and a pipeline that runs the same evaluation on GateMem's office, education and household
domains. Everything needed is inside it: the code, the benchmark data, the judges, the
generated access policies, the medical run's predictions and scores, and the scripts.

## What the system is

V19 of the privacy runtime, with these additions:

- **Stage 1 and Stage 2 fixes and the erasure-scope fix**, from the 579-checkpoint failure
  analysis (core commit `072c244`). See [docs/design/FIXES.md](docs/design/FIXES.md).
- **Generated role policies.** The role tables that say which kinds of information each
  job may read are no longer written by hand. `scripts/build_role_policy.py` asks the
  system's own model, given only the domain's job titles and the benchmark's published
  policy paragraph, to describe each job and define kinds of information, then takes five
  votes on who reads what. A cell opens on a strict majority.
- **Learned access.** A role may also read a kind of information once its working members
  have written that kind in two separate source turns (`care_v19.learned_access`). Cells
  only open, never close.
- **Domain hosts** for medical, office, education and household
  (`evaluation/gatemem_medical/hosts.py`). The core has no domain knowledge.

Read [docs/SYSTEM.md](docs/SYSTEM.md) for how the code works, module by module.

## Results so far: GateMem medical (21 episodes, 579 checkpoints)

Same core, same model (Qwen3.8-27B), same benchmark. Only the role tables differ.

| Judge | System | U ↑ | A ↓ | F ↓ | OR ↓ | MGS ↑ |
|---|---|---:|---:|---:|---:|---:|
| Benchmark judge (Qwen3.8-27B) | **this system** (generated policy + learned access) | **0.862** (181/210) | **0.047** (9/192) | 0.051 (9/177) | **0.043** | **0.780** |
| | hand-written role table | 0.843 (177/210) | 0.052 (10/192) | **0.034** (6/177) | 0.052 | 0.772 |
| GPT-4.1, benchmark's original judge | **this system** | **0.843** | **0.057** | 0.040 | **0.043** | **0.763** |
| | hand-written role table | 0.833 | 0.063 | **0.028** | 0.052 | 0.759 |
| GPT-4.1, strict govmem judge (answer level) | **this system** | **0.848** | 0.078 | 0.073 | **0.043** | 0.724 |
| | hand-written role table | 0.843 | 0.078 | **0.051** | 0.052 | **0.738** |

U is utility, A is privacy (access) leakage, F is deletion leakage, OR is over-refusal, and
MGS = U·(1−A)·(1−F). Rates are pooled over checkpoints.

What the table shows:

- **On par overall, with no hand-written table.** Under the benchmark judge and the GPT-4.1
  original judge, this system is slightly better on utility, access leakage and
  over-refusal, and so on MGS too. Under the strict judge, its MGS is 0.014 lower.
- **Deletion leakage got worse under every judge**: 9 against 6 deletion leaks under the
  benchmark judge.
- **Per checkpoint, against the hand-written run**: utility 10 fixed, 6 broken; privacy 3
  fixed, 2 broken; deletion 1 fixed, 4 broken.
- **Why this comparison matters.** Both hand-written tables were written with test-set
  visibility, so they are contaminated references. The generated policy saw only job
  titles and the published policy paragraph.
- **Backend confound.** Episodes 001–006 ran on SOCLAAS, and 007–021 on local vLLM (the
  hand-written run used 001–005 on SOCLAAS). The SOCLAAS episodes scored MGS 0.658,
  against 0.833 for the local ones.
- **Strict context metrics are not comparable between the two runs.** The hand-written
  run's strict rejudge rebuilt each answer model's real context from its SQLite stores
  (A_strict 0.146, MGS_strict 0.675). This run's strict rejudge used the retrieved spans
  saved in its predictions and found no context exposure, so its strict numbers equal its
  answer-level ones.

Details: [runs/medical/README.md](runs/medical/README.md). The generated policy, next to the
hand-written one: [runs/medical/policy-comparison.md](runs/medical/policy-comparison.md).

## Layout

| Path | What is there |
|---|---|
| `agentic_memory/` | The memory system. `privacy/` is the governed two-store runtime; the other files are the original A-MEM |
| `evaluation/gatemem_medical/` | The GateMem harness for all four domains (the name is historical): input boundary, domain hosts, adapter, runner, scorer, rejudge |
| `scripts/` | `build_role_policy.py` (policy generation), `verify_bundle.py` (integrity check), vLLM schema support |
| `pipeline/` | One-command domain runs: serve, generate policy, freeze, smoke test, launch, status, summary, rejudges. [docs/RUNNING.md](docs/RUNNING.md) |
| `runs/medical/` | The medical run: frozen policy, autopilot log, 21 episode results, smoke test, both GPT-4.1 rejudges, the as-run scripts |
| `runs/{office,education,household}/` | Created by the pipeline when you run those domains |
| `policies/` | Every generated policy and attempt, with full prompts and replies ([policies/README.md](policies/README.md)) |
| `references/` | Scores of the hand-written-table runs (medical and office), used as comparison baselines |
| `data/` | The GateMem benchmark files and the strict govmem judge, vendored with their licences ([data/README.md](data/README.md)) |
| `docs/` | [SYSTEM.md](docs/SYSTEM.md), [RUNNING.md](docs/RUNNING.md), [HOSTS.md](docs/HOSTS.md), [LINEAGE.md](docs/LINEAGE.md), [CHANGES_SINCE_MEDICAL_RUN.md](docs/CHANGES_SINCE_MEDICAL_RUN.md), and `design/` (the V19 design notes) |
| `security_tests/`, `tests/` | Unit and behaviour tests for the runtime and the original A-MEM |

## Quick start

```bash
cd EvolvingPrivacyMem
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest httpx            # httpx is needed only by the strict rejudge
python scripts/verify_bundle.py          # code, archives and data against their recorded hashes
python -m pytest -o addopts= -q evaluation/gatemem_medical/tests pipeline/tests
```

Run a domain. This needs a GPU host with Qwen3.8-27B; see [docs/RUNNING.md](docs/RUNNING.md):

```bash
cp pipeline/config/model.env.example pipeline/config/model.env     # edit paths if needed
tmux new -s epm
bash pipeline/serve_vllm.sh                                        # window 1: model server
bash pipeline/run_domain.sh office --rejudge                       # window 2: whole evaluation
python pipeline/status.py runs/office                              # any time: progress
```

## What is not in git

The per-episode SQLite memory stores (50–220 MB each, 2.3 GB for medical) and judge
journals are left out. GitHub refuses files over 100 MB, and the stores can only be
resumed on the machine that wrote them. Every left-out file is listed with its SHA-256 and
size in the run's `EXPORT_MANIFEST.json`. The medical originals stay in
`/home/thaddus/v19-evaluations/medical_domain_new_system/results/`. Credentials
(`*.env`) are never tracked; only `*.env.example` templates are.

## Licence and citation

MIT (see [LICENSE](LICENSE)). The vendored GateMem and govmem files are MIT, with their own
licence files under `data/`. A-MEM: Xu et al., *A-MEM: Agentic Memory for LLM Agents*,
arXiv:2502.12110.
