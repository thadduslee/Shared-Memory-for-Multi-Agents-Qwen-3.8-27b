V20 full evaluation using the existing localhost:19000 API.

Memory/answers: qwen3.8:27b; separate judge: gemma4:26b.
CPU embeddings; no GPU server starts and no OpenRouter calls.
Read progress.json, summary.json and run.log; model-trace.jsonl records calls.
Source latency includes scheduled host work. Full coverage is 579 scored checkpoints.
The provider/judge change prevents a controlled v19 comparison.

Resume stopped work with the SAME frozen launcher and run directory:

/home/thaddus/gatemem/.venv-nb/bin/python -u /home/thaddus/a-mem-variant/runs/gatemem-medical-v20-local-full-20261102T120810+0800/frozen-source/analysis/workflow-v20/local_session.py run /home/thaddus/a-mem-variant/runs/gatemem-medical-v20-local-full-20261102T120810+0800 --resume
