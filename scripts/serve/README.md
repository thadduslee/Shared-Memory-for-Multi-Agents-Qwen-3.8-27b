# Serving on H200s

Written for: whoever starts a real run on this box.

The orchestrator has five nodes. **Exactly one needs a GPU.**

| Node | Backend | GPU |
|---|---|---|
| Architect / Developer / Critic | OpenRouter (DeepSeek) | — |
| **Medical Evaluator** | **local vLLM** | **1 × H200** |
| Judge | OpenAI (`gpt-4.1`) | — (or 1 × H200, optional) |

So the minimum for `python main.py --real` is **one H200**.

```bash
scripts/serve/install_vllm.sh        # once
scripts/serve/serve_evaluator.sh     # picks a free GPU, waits until ready
scripts/serve/healthcheck.sh         # five checks; run this before a long run
```

Then export what the script prints, and go.

## Why one GPU, when `serve_qwen38.sh` uses two

Qwen3.8-27B in bf16 is 27e9 × 2 bytes ≈ **54 GB** of weights. An H200 has
**141 GiB**. The repo's existing `serve_qwen38.sh` sets
`--tensor-parallel-size 2` because it was written for L40S cards at 48 GB each,
where 54 GB genuinely does not fit on one card and the weights *had* to be
sharded. On an H200 the model fits with ~73 GB left for KV cache.

TP=1 is not just sufficient here, it is the better configuration. Tensor
parallelism splits every layer across GPUs and pays an all-reduce per layer; it
buys capacity you don't need and costs latency you do. If you want more
throughput, run more single-GPU replicas (`scale_evaluator.sh`), not a wider TP.

Measured on one H200, `max_tokens=128`:

| concurrency | wall | tok/s | median latency |
|---:|---:|---:|---:|
| 1 | 0.26 s | 58 | 0.26 s |
| 8 | 0.33 s | 360 | 0.32 s |
| 24 | 0.43 s | 848 | 0.43 s |
| 48 | 0.53 s | 1372 | 0.51 s |

Still scaling at 48 with latency roughly flat, which is why the script suggests
`VLLM_MAX_CONCURRENCY=48`. The shipped default of 12 was sized for "~2 in flight
per GPU" on a 6-GPU L40S cluster and leaves an H200 mostly idle.

## The one thing that will silently ruin a run

`config.EVALUATOR_MODEL` defaults to the bare slug `qwen3.8-27b`.
`config.vllm_chat_template_kwargs` only emits `{"enable_thinking": false}` for a
model whose name starts with `Qwen/` or `qwen/`:

```
vllm_chat_template_kwargs("qwen3.8-27b")      -> None
vllm_chat_template_kwargs("Qwen/Qwen3.8-27B") -> {'enable_thinking': False}
```

Qwen3.8 has thinking **on** by default. Left on, the evaluator reasons past the
read timeout and records an empty answer for every checkpoint that retrieved
successfully — which reads downstream as a *retrieval* failure, not a timeout.
That is the `render_degraded` mode the Auto Research README describes costing a
3-hour, 12.5M-token run that reported `best MGS=0.8366` with the evaluator
effectively dead.

These scripts therefore serve under the **prefixed** name and
`healthcheck.sh` refuses to pass unless `EVALUATOR_MODEL` matches it. This is
also the real bug behind the pre-existing test failure in
`Auto Research/tests/test_vllm_thinking.py` — that test asserts the bare slug
disables thinking, and it does not.

`--reasoning-parser qwen3` is the second, independent guard: it splits any
reasoning that *is* produced into `reasoning_content`, leaving `content` as the
answer alone. Both guards exist because each one alone has already failed here.

## Scripts

| | |
|---|---|
| `install_vllm.sh` | builds the `gatemem-vllm` conda env (idempotent) |
| `serve_evaluator.sh` | the required node. 1 GPU, port 8000 |
| `serve_judge.sh` | optional local judge. 1 GPU, port 8001 |
| `scale_evaluator.sh N` | N single-GPU replicas on ports 8000..8000+N-1 |
| `healthcheck.sh [port]` | five pre-flight checks; non-zero exit on failure |
| `status.sh` | GPUs, which are free, what is serving |
| `stop.sh` | SIGTERM then SIGKILL, and shows GPU state after |

GPUs are **auto-selected** from what is actually free rather than hardcoded —
this box is shared, and `serve_qwen38.sh`'s fixed `CUDA_VISIBLE_DEVICES=4,5`
will eventually land on someone else's job. Override with
`CUDA_VISIBLE_DEVICES` if you want a specific card.

## Before trusting a local judge

`serve_judge.sh` exists so the loop can run with no OpenAI key, but read the
header in that script first. Two hazards:

1. **Self-judging.** Under `JUDGE_IMPL=gatemem` the judge is *primary* on
   utility — it can overturn the rule matcher. If it is the same model as the
   evaluator, the system grades its own homework and a shared blind spot
   becomes an inflated score. The script refuses to start in that configuration
   unless you set `JUDGE_ALLOW_SELF_JUDGE=1`.
2. **Comparability.** GateMem's published numbers use `gpt-4.1`. A local judge
   gives internally-consistent numbers (fine for "did iteration N beat N−1")
   that are *not* comparable to the paper or `outputs/`. The repo already keeps
   an `outputs_qwenjudge_backup/` tree for exactly this reason.

Steer the loop with a local judge; re-judge with `gpt-4.1` before reporting.

## Environment notes

Three conda envs, deliberately separate:

| env | for |
|---|---|
| `gatemem` | the benchmark (`bench/`, `scripts/`) |
| `gatemem-auto` | the orchestrator (`Auto Research/`) |
| `gatemem-vllm` | serving only |

All built from `conda-forge` with `--override-channels`: conda's default
channels require accepting Anaconda's Terms of Service, which is a licensing
decision a setup script should not make for you.

`common.sh` exports `LD_LIBRARY_PATH="$VLLM_ENV/lib"` before launching. This is
required, not cosmetic — this box's system `libstdc++` predates `CXXABI_1.3.15`,
which conda-forge's `libicui18n` needs, so the `vllm` entry point dies on
`import sqlite3` without it. Confusingly, `python -c "import sqlite3"` succeeds
in the same env, which makes it look like a vLLM bug rather than loader order.

## Disk — weights live on `/node2`, not `/home`

`common.sh` sets `HF_HOME=/node2/alfred/huggingface`.

This is not a preference. `/home` is an 879 GB disk that was at **87% before**
the first 52 GB model landed on it, and at **95% after**. A judge model is
another ~60 GB and would simply not have fitted. `/node2` is 24 TB with 6.4 TB
free.

Override with `HF_HOME` if you want a different location. Note that
`HF_HUB_CACHE` and `TRANSFORMERS_CACHE` silently *override* `HF_HOME` when set,
which is the usual way a model ends up back on the wrong disk — `common.sh`
unsets both.

Anything outside these scripts that pulls weights (a bare `vllm serve`, a
`transformers` call, `huggingface-cli download`) needs `HF_HOME` exported too,
or it will download a second copy to `~/.cache/huggingface`.
