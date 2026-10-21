# a_mem_gov: A-Mem with governed, time-ordered memory

`bench/agents/a_mem_gov.py` (bench agent `a_mem_gov`) builds on the A-Mem baseline (`bench/agents/a_mem.py`). It
keeps A-Mem's notes, links and one LLM call per turn. Every change is a switch in `--a_mem_gov_features`.

| Feature | What it does |
|---|---|
| `chrono` | Show retrieved notes oldest first, with turn id, time, speaker role and addressee. A-Mem shows them by score with no attribution, so the answerer copied refusals that were said to someone else. |
| `asker` | Reserve up to 6 of the 20 slots for notes about the requester: their own turns, turns addressed to or naming them, and turns that changed their access. Topic retrieval never fetches assignment or consent turns. |
| `evolve` | The per-turn call also emits operations on earlier notes: delete, supersede, restrict, grant, revoke. A delete closes the note's validity, leaves a masked copy, and tombstones the deleted values in later notes. A restriction splits off a copy with the values replaced by `[withheld]`. |
| `gate` | Each retrievable copy is a vector [content ; sensitive categories ; denied people ; validity in turns], and a query is [content ; -disallowed categories ; -requester ; as-of turn]. One inner product decides whether a copy is released; `--a_mem_gov_tau` (default 0.5) sets the threshold. Validity is a hard constraint at any tau. |
| `policy` | One call per episode turns the domain's policy text into default rules by role. Off by default; see below. |
| `owner` | Each note gets an owner (the patient or project it is about), and roles limited to their linked subjects can't see sensitive notes about others. Off by default; see below. |

Tests: `python -m pytest bench/tests/test_a_mem_gov.py` (22 tests, all offline).

## Running

```bash
# .env in the repo root holds SOCLAAS_BASE_URL / SOCLAAS_API_KEY (or point AMG_API_BASE at a local vLLM)
export AMG_PYTHON=~/miniconda3/envs/gsm/bin/python      # numpy, requests, torch, transformers + govmem deps
export HF_HOME=<cache with sentence-transformers/all-MiniLM-L6-v2>
export SSL_CERT_FILE=<CA bundle>                         # only if the endpoint's TLS chain needs one

scripts/a_mem_gov/run.sh medical                         # one domain -> outputs_amem_gov/medical_qwen38_27b_a_mem_gov/
scripts/a_mem_gov/judge.sh medical a_mem_gov outputs_amem_gov/medical_qwen38_27b_a_mem_gov/predictions.jsonl
scripts/a_mem_gov/run_all.sh v2a                         # all four domains in parallel, then judge each
```

- **Local vLLM:** `AMG_API_BASE=http://localhost:8000/v1 AMG_MODEL=Qwen/Qwen3.8-27B AMG_CONCURRENCY=24`. Set
  `GOVMEM_JUDGE_BASE_URL` / `GOVMEM_JUDGE_MODEL` so the judge uses it too.
- **Thinking:** the answerer uses the server default (thinking on, as in every baseline). The per-turn write
  calls send `enable_thinking=false`. The judge is govmem's (thinking off).
- **SoCLaaS rate limits:** it rejects requests above about 12–16 in flight ("Per-API key queue is full"), so keep
  4 domains x 3 episodes. `run.sh` retries 20 times and resumes up to 4 times. Daytime latency is about 3x
  overnight: a four-domain run is about 11 h by day, 4–5 h overnight, and about 1–1.5 h on a dedicated H200
  (estimate).
- **Write cache:** per-turn replies are cached in `outputs_amem_gov/write_cache_<run>.jsonl`, keyed by prompt.
  A rerun that changes only query-side settings (tau, `chrono`, `asker`, `gate`) pays only for answer calls.
- **Cost:** on SoCLaaS a four-domain run is about $25. Its spend limits are $50 a day and $500 a month.

## Results so far (Qwen3.8-27B answerer and judge; govmem judge for every system)

### Medical, all four changes (579 checkpoints)

| System | U | A | F | Over-refusal | MGS | Strict MGS |
|---|---|---|---|---|---|---|
| A-Mem, rerun on the same endpoint (control) | .614 | .297 | .040 | .267 | .415 | .000 |
| **a_mem_gov** | **.800** | **.219** | **.006** | **.110** | **.621** | **.367** |
| long_context | .919 | .193 | .006 | .038 | .738 | .000 |
| GSM v20auth | .676 | .068 | .017 | .210 | .620 | .387 |

Against the control, a_mem_gov is better on 86 checkpoints and worse on 26 (sign test p ≈ 1e-8).

### Medical ablations and gate sweep

| Run | U | A | F | MGS | Strict A | Strict MGS |
|---|---|---|---|---|---|---|
| all four, tau 0.5 | .800 | .219 | .006 | .621 | .458 | .367 |
| without chrono | .671 | .234 | .006 | .511 | .495 | .287 |
| without asker | .729 | .271 | .011 | .525 | .516 | .297 |
| without gate | .838 | .224 | .011 | .643 | .917 | .053 |
| without evolve | .843 | .203 | .006 | .668 | .948 | .001 |
| tau 0.97 | .838 | .266 | .011 | .609 | .531 | .328 |
| tau 1.0 (546/579 checkpoints) | .848 | .273 | .006 | .612 | .842 | .112 |
| tau 2.0 | .810 | .281 | .011 | .575 | .891 | .070 |

- `chrono` and `asker` carry the answer-level gain. `gate` and `evolve` carry the strict score.
- Loosening the gate gains at most about .04 U, around the answerer's run-to-run noise (about ±.03 U). It doubles
  prompt exposure.
- The tau ≥ 1 runs before commit `4167f4f` released deleted copies and are invalid.

### Four domains, v1 (answer-level MGS / strict MGS)

| System | Medical | Office | Education | Household |
|---|---|---|---|---|
| A-Mem | .420 / .000 | .517 / .009 | .185 / .001 | .396 / .000 |
| a_mem_gov v1 | .621 / .367 | .399 / .170 | .191 / .086 | .272 / .220 |
| long_context | .738 / .000 | .586 / .000 | .566 / .000 | .578 / .000 |
| GSM v20auth | .620 / .387 | .594 / .417 | .428 / .169 | .493 / .212 |

v1 loses utility on office and household because of a deletion-tombstone bug. Capitalised runs inside deleted
values ("Project Harbor", "Sunday") were masked as `[deleted]` in every later note. The current code keeps a
piece of a deleted value as a tombstone only if it contains digits, or it is a name found in at most 2 notes;
calendar words never are. **That fix has not been run yet:** `run_all.sh v2a` is the run to do next.
Medical without `evolve` scoring higher (.668) is the same bug.

## Policy defaults and owner gate: tried, off by default

`policy` and `owner` were run on all four domains for the first ~40 checkpoints each, then stopped. Utility
questions went unanswered 2–5x as often as in v1:

- **Owner gate:** most owners are cases or items nobody is linked to by default ("Cedar Review", "Oak Cleaning").
  Care roles without a recorded assignment are never linked. Evidence released to the answerer fell from 60–98
  notes to 0–21. Link and owner names also normalise differently (`project_atlas` vs "project atlas"), which is
  unfixed.
- **Policy defaults:** the 11 categories are too coarse for "need-to-know within your role". For example,
  "professors may not see financial" blocks a professor's own project stipend.
- **What it targets:** cross-subject leaks total 14 privacy checkpoints in v1 (medical 7/20, office 2/21,
  education 0/12, household 5/23).

A narrower owner gate is the untested next step: only notes owned by a person, and only for requesters who are
subjects or outsiders (patients, family, guests, residents). Staff roles and case or project owners stay ungated.
