V21 full evaluation on cluster GPUs 2,3,4.

Qwen 3.8 27B BF16 tensor parallel on GPUs 2-3, port 38721.
Qwen3 14B BF16 judge on GPU 4, port 38722.
Same frozen validated V21 runtime; fresh database and predictions.
No aggregate source/answer deadline. CPU embeddings. Full evolution enabled.

Attach: tmux attach -t gatemem-v21-gpu234-20261102T182855+0800

Logs: memory-server.log, judge-server.log, run.log, model-trace.jsonl.

Active continuation: citation transport repair 6901e0e84ab5a03c78bfd7650f0b6268019b125afa99efb7d0b75e01994aafa5.
The original frozen-source and launch.py remain preserved. Active source/launcher are recorded in active-invocation.json.
For a future resume after the worker stops, run:

/home/thaddus/a-mem-variant/runs/gatemem-medical-v21-gpu234-full-20261102T182855+0800/resume-current.sh
