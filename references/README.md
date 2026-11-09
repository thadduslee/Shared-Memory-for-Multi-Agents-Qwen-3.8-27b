# Reference runs (hand-written role tables)

These two runs used the same core as this system (`072c244`). The difference is that
their role tables were written by hand, with test-set visibility: the office table was
written after reading checkpoints' expected actions by role, and the medical table was
drafted while diagnosing pilot checkpoints and then widened. **Treat their scores as
contaminated references, not as baselines to beat by tuning.** `pipeline/compile_summary.py`
uses them by default to list per-checkpoint changes for medical and office.

Only scores and reports are kept here: per-episode `scores.jsonl`, `summary.json` and
`exit-code.txt`, the pooled summary, and the rejudge reports and verdicts. Their
predictions, logs and stores stay in the original run directories. `EXPORT_MANIFEST.json`
in each folder gives the source and the hashes.

| Folder | Source | Benchmark judge | GPT-4.1 original judge | GPT-4.1 strict |
|---|---|---|---|---|
| `medical_domain_original/` | `/home/thaddus/v19-evaluations/medical_domain_original` (21 episodes, 579 checkpoints; 001–005 on SOCLAAS) | U 0.843, A 0.052, F 0.034, MGS 0.772 | U 0.833, A 0.063, F 0.028, MGS 0.759 | U 0.843, A 0.078, F 0.051, MGS 0.738; A_strict 0.146, MGS_strict 0.675 (contexts rebuilt from the stores) |
| `office_domain/` | `/home/thaddus/v19-evaluations/office_domain` (17 episodes, 547 checkpoints; 001–005 on SOCLAAS, 006–017 local) | U 0.747 (115/154), A 0.152 (26/171), F 0.054 (12/222), OR 0.097, MGS 0.599, pooled here (that run wrote no pooled summary) | U 0.649, A 0.146, F 0.050, MGS 0.527 | U 0.701, A 0.164, F 0.081, MGS 0.539; A_strict 0.187, F_strict 0.279, MGS_strict 0.411 |

All 547 office checkpoints are scored, but only 8 of the 17 episode folders have an
`exit-code.txt`.

To pool the office per-episode scores with the benchmark judge yourself:

```bash
python -c "import sys,glob; sys.path.insert(0,'pipeline'); import common; \
rows=[r for f in sorted(glob.glob('references/office_domain/results/*/scores.jsonl')) for r in common.read_jsonl(f)]; \
print(common.headline(rows))"
```
