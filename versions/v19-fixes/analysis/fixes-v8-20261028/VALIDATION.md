# Question-binding failure and model latency

The preserved full v7 run failed at source t065 in boundary 2. Its entire text
was a question. All three proposals tried to bind either the whole question or
its trailing clause as factual memory. The host correctly rejected them. A
generic correction plus question offsets did not cause the model to remove the
unsupported binding. No invalid memory was published.

The v8 wire schema excludes sources without candidate assertion ranges from
bind_information.source_id, while retaining those same sources as permission,
participation and lifecycle evidence. Mixed sources retain their assertions.
The independent host overlap check remains mandatory. Rejections now include
the assignment ID, available assertion ranges and an explicit removal instruction.
There is no fallback that drops operations or accepts a rejected proposal.

The model receives less redundant historical binding evidence and is prompted
to use short checks/explanations. Canonical records are unchanged. Full live
source text, original authorization text, assignment evidence, restriction state,
binding selectors, necessity and restriction applicability outcomes are retained.
Progress shows permission elapsed time and attempts separately from evolution.

## Evidence

The original 64 completed sources spent 3,640.3 seconds in permission processing
and 461.7 seconds in evolution. Including t065's failures, the 74 permission
attempts used 1,234,408 input tokens and 91,356 output tokens; 10 attempts were
rejected. Model calls consumed 3,770.1 seconds. The earlier v7 cache fix targets
authorization SQL work; its two-second retrieval profile excluded model calls.
It did not establish a fast end-to-end benchmark runtime.

[replay_failure.py](replay_failure.py) copies both source databases using SQLite
backup and opens the copies under explicit permission version 8. No original
run database, manifest, prediction or score is modified, and no official query
is replayed. The first sandboxed attempt could not connect to localhost; the
separate real-model diagnostic used the local model with network access.

The [real-model replay report](failed-prefix-replay-real/report.json) records
success in one call: 32.44 seconds, 27,947 input tokens and 583 output tokens.
The new payload was 83,408 bytes versus 93,009 bytes for a same-state reconstruction
of v7's historical binding evidence (about 10% smaller). The original rejected
t065 calls totaled 143.73 seconds, with 1,151/1,142/1,142 output tokens. This is a
focused copied-prefix diagnostic under a new permission version, not a controlled
whole-run speed comparison or an official benchmark score. Generation remains
the dominant cost; there is no measured multi-fold whole-run speedup claim.

The security and evaluation suites cover question-only schema choices, preserved
question authority evidence, mixed events, rejection/repair, full restriction
retention, canonical evidence immutability, and the existing security contracts.
Their final logs are stored beside this document.
