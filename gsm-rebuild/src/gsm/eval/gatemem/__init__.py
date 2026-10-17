"""GateMem evaluation adapter.

Integration boundary with the GateMem benchmark that ships in this repository
(``bench/data/<domain>/{episodes,checkpoints}.jsonl``, scored by
``bench/scripts/score_predictions.py``). GSM does not re-implement GateMem's
formulas; the scoring step imports the official scorer when it is available.

Method side (``sanitize``, ``adapter``) never sees scoring-only annotations.
Scoring side (``scoring``) is the only module that reads them, after the run.
"""
