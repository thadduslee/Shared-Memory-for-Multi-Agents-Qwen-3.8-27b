# CIMemories (vendored)

Unmodified copy of https://github.com/facebookresearch/CIMemories at commit
`e39806e92ec66db120c0f3b73c9cb105e7ff5314` (fetched 2026-10-01), without `.git` and the README figure.
License: CC BY-NC 4.0 (see `LICENSE`); research use only.

GSM uses it read-only: `data/data_openai_gpt-oss-120b.json` (profiles + task contexts),
`eval/prompts.yaml` (task and reveal prompts) and `eval/metrics.py` (the official violation/coverage
computation). The gold labels are **not** released upstream or on Hugging Face (`facebook/CIMemories`
`test.csv` has an empty `label` column); `gsm cimemories label` regenerates them with the procedure of
`data/persona_gold_labels.py`. See `gsm-rebuild/docs/CIMEMORIES.md`.
