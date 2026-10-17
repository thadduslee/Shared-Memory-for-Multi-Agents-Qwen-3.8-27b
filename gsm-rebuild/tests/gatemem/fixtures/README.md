# GateMem-format fixtures

Hand-written, synthetic fixtures in the GateMem `episodes.jsonl` / `checkpoints.jsonl`
schema. They are **not** taken from the official dataset. Every scoring-only field
(`query_type`, `expected_action`, `judge_spec`, `attack_type`, `leak_targets`, plus an
extra unknown field) carries a unique `CANARY-*` string so tests can prove none of it
reaches ingestion, retrieval, policy, generation or verification.
