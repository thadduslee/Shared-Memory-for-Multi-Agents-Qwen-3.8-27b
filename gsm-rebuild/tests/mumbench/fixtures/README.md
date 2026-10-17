# MUMBench-format fixtures

Hand-written, synthetic interactions in the schema printed in the AIM paper (arXiv 2609.12320,
Appendix A.5). Not taken from MUMBench (which is unreleased). `input_type`, `memory_actions` and
`_metadata` carry `CANARY-*` strings so tests can prove none of it reaches a method.
`predictions.jsonl` is a hand-made method output used to check the scorer's arithmetic.
