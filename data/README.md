# Vendored benchmark files

Everything the pipeline reads from outside this repository is copied here, byte for byte,
so a clone can run without `/home/thaddus/GateMem` or `/home/thaddus/a-mem-gov`. Every file's
SHA-256 is listed in [`EXPORT_MANIFEST.json`](EXPORT_MANIFEST.json), and
`python scripts/verify_bundle.py` checks them.

## `gatemem/`: the GateMem benchmark (MIT, [`gatemem/LICENSE`](gatemem/LICENSE))

From `https://github.com/rzhub/GateMem`, local checkout `/home/thaddus/GateMem` at commit
`d3b73fece95691704a68bd5f2f94a70207479f7e`. That checkout had no local changes to these files.

| Path | Used by |
|---|---|
| `bench/data/{medical,office,education,household}/{episodes,checkpoints}.jsonl` | the harness, the scorers and the policy builder |
| `bench/domains.py` | `get_query_policy_block()`: the published policy paragraph the policy builder reads |
| `bench/__init__.py`, `bench/eval/{__init__,metrics,scorer}.py` | the frozen benchmark scorer (`score_one`, `aggregate`) |

Checks done when these files were copied:

- The medical files are identical to the `frozen-dataset` the medical run used
  (`a-mem-variant/evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/frozen-dataset`).
- The office files are identical to `v19-evaluations/office_domain/frozen-dataset`.
- The data files for all four domains are identical to `/home/thaddus/a-mem-gov/bench/data`, which the strict judge reads.
- Dataset sizes: medical 21 episodes, 579 checkpoints, 567 boundaries; office 17, 547, 467;
  education 30, 540, 518; household 23, 552, 545 (`runner.PINNED`).

## `govmem-strict-judge/`: the strict judge (MIT, [`govmem-strict-judge/LICENSE`](govmem-strict-judge/LICENSE))

From `/home/thaddus/a-mem-gov` at commit `a297102d7e707c616c99a8e274f912d6c8e81b3d`: `govmem/src/govmem/`
and `bench/prompts/`. These are the same bytes as the `frozen-govmem/` copies in the medical
and office strict rejudges. The two files that define strict scoring have these hashes:

| File | SHA-256 |
|---|---|
| `src/govmem/eval/judge.py` | `df7a4f6ded9c327f44c2f6ea49c68ef54e143ed9e8b48f9846651fb7addb75c3` |
| `bench/prompts/judge_prompt.txt` | `054f2db5de0f0537671ff4fbb3f88dd9e694ac8f7738d484648aca8d88ee111a` |

`pipeline/rejudge_strict.py` imports it; nothing else does.
