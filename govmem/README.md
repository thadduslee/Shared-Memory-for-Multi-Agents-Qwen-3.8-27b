# govmem

A governed shared memory for several humans and agents, whose structure organises itself during a conversation.
Models interpret; deterministic code decides who may see, change or remove what. No model is trained.
The contract is [`SPEC.md`](SPEC.md); §18 lists where this first build differs from it.

## Layout

```
src/govmem/
  core/        keyring (only path to plaintext), fixed schema, store, operations + authority
  interpret/   roster and name resolution, label / rescope / remove calls, proposals
  organize/    enrich, link, evolve neighbours, cluster, consolidate (A-Mem style, audience-safe)
  retrieve/    relevance, candidates, gate, budget, link expansion, suggested action
  release/     bounding the answerer
  adapters/gatemem/   annotation wall, GateMemAgent, benchmark answerer, runner
  eval/        judge (rule + LLM, answer-level and strict), baseline rescoring
tests/         invariants I-1..I-16, vocabulary lint, annotation canary
```

## Run

```bash
cd govmem
~/miniconda3/envs/gsm/bin/python -m pytest -q                 # no model, no network

source scripts/env_soclaas.sh                                 # every role on SoCLaaS qwen3.8:27b
~/miniconda3/envs/gsm/bin/python -m govmem.adapters.gatemem.run \
    --domain medical --episodes 2 --run-dir runs/smoke_medical_2ep --workers 2

# the same checkpoints, a baseline, the same judge
~/miniconda3/envs/gsm/bin/python -m govmem.eval.rescore --run-dir runs/smoke_medical_2ep \
    --name long_context --predictions ../outputs/medical_qwen38_27b_long_context/predictions.jsonl
```

A run directory holds `run.json` (manifest), `episodes/<id>/{memory.db, evidence.jsonl, summary.json}`,
`predictions.jsonl`, `verdicts.jsonl`, `scores.json` (answer-level and strict) and `health.json`
(per-call-type parse failures, fallbacks, budget refusals; `eligible: false` when a threshold is crossed).
Model replies are cached under `cache/`, so a rerun repeats no calls. `--no-model` runs the deterministic fallbacks.

Feature switches for ablations: `GOVMEM_FEATURE_<NAME>=0`, names in `src/govmem/config.py::Features`.
