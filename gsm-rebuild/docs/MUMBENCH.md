# MUMBench evaluation adapter

MUMBench (Microsoft; introduced with AIM, [arXiv 2609.12320](https://arxiv.org/abs/2609.12320)) is a
multi-user memory benchmark: 672 chronologically ordered interactions from 93 users in four domains (Coding 150,
Customer Support 218, Education 160, Travel Planning 144). The memory store is shared across users. For each
interaction the system must pick the memory operations (create / read / update / delete / no-op), extract the
memory, classify it as **private** (its user only) or **public** (all users), and never surface another user's
private memory, including under 60 adversarial queries.

## Data is not released

The paper calls MUMBench "the first public dataset", but as of 2026-10-01 it gives no link, and nothing named
MUMBench is on GitHub or Hugging Face. The adapter follows the schema printed in Appendix A.5. To run it you
need the data from the authors (corresponding authors are at Microsoft; see the paper).

Expected layout: one file per domain, `*.json` (a list) or `*.jsonl`, in any directory passed as `--data-dir`.
`gsm mumbench check --data-dir DIR` prints per-domain counts to compare with the paper's (Tables 3-4).

## Integration boundary

* **Method side** (`gsm.eval.mumbench.data.load_for_method`), allowlist: `interaction_id`, `input`, `timestamp`,
  `user_id`, `agent_id`, `run_id`, plus the domain and stream position. `input_type` (query vs statement gives
  away the operation), `memory_actions` and `_metadata` stay behind (canary test in `tests/mumbench`).
* **Predictions**: `predictions.jsonl`, one line per interaction with the method's actions. Read results name
  their `source_interaction_id` (GSM provenance), so the scorer decides from the gold labels, not from the
  method's own visibility claim, whether a retrieved item was someone else's private memory.

## Metrics (re-implemented; no official scorer is released)

| Metric | Computation | Paper |
|---|---|---|
| `strict_operation_accuracy` | predicted operation multiset == gold multiset | "strict operation accuracy" (AIM 58.8%) |
| `count_match` | same number of actions | "count match" |
| `visibility_match` | private/public agreement over create/update pairs (greedy best match on content) | "visibility match" (AIM 96.0% classification) |
| `tag_overlap` | Jaccard of tag sets over the same pairs | "tag overlap" |
| `content_fidelity` | embedding cosine over the same pairs (`--embedder`; null without) | "content fidelity" |
| `unauthorized_retrieval_rate` | retrieved items from another user's private-gold interaction / all retrieved | access control (paper reports it qualitatively) |
| `adversarial_leak_rate` | interactions with at least one such item / all | access control |

Not implemented: *state-aware* operation accuracy (re-basing gold on the method's own earlier state) and
*judge-decision* accuracy (a gpt-4.1-mini verdict); the paper does not specify them precisely enough to
reproduce. Domain numbers are macro-averaged, as in the paper's "Avg" column.

## GSM method

Not wired yet; it needs the data to be useful. The mapping is direct: users are principals of one tenant;
`private` = owner-only, `public` = everyone in the tenant; reads are governed queries as the asking user; deletes
and updates are GSM operations authorized as the requester. The v7 interpreter's audience labels collapse to
`speaker` vs `everyone`, which makes MUMBench a much coarser access-control test than GateMem.
