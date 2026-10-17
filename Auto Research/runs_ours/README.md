# Our runs (2026-09-17 → )

Grouped by try, in order. Each try holds `medical/` and `office/` when both exist.
Git-tracked run dirs (`runs_final`, `runs_real_100iter_v4`, `runs_smoke*`, `runs_test*`, `runs_verify*`) are not ours and were left in place.
`runs/` itself is the default RUNS_DIR and predates our work; untouched.

| try | what | domain | was | iters | best dev MGS (iter) | U / A / F |
|---|---|---|---|---|---|---|
| 01_local_bringup_2026-09-17 | first local Qwen bring-up (dsh/http transports, smoke) | medical/local_smoke | `runs_local_smoke` | 1 | - | - |
| 01_local_bringup_2026-09-17 | first local Qwen bring-up (dsh/http transports, smoke) | medical/local_smoke2 | `runs_local_smoke2` | 1 | - | - |
| 01_local_bringup_2026-09-17 | first local Qwen bring-up (dsh/http transports, smoke) | medical/local_http | `runs_local_http` | 1 | 0.2500 (1) | 0.5000 / 0.0000 / 0.5000 |
| 01_local_bringup_2026-09-17 | first local Qwen bring-up (dsh/http transports, smoke) | medical/local_full | `runs_local_full` | 1 | - | - |
| 01_local_bringup_2026-09-17 | first local Qwen bring-up (dsh/http transports, smoke) | medical/local_20iter | `runs_local_20iter` | 2 | 0.6111 (2) | 0.6111 / 0.0000 / 0.0000 |
| 01_local_bringup_2026-09-17 | first local Qwen bring-up (dsh/http transports, smoke) | medical/local_20iter_critic16k_aborted | `runs_local_20iter_critic16k_aborted` | 1 | 0.6797 (1) | 0.8333 / 0.0588 / 0.1333 |
| 02_hybrid_2026-09-18 | DeepSeek agents on OpenRouter + local evaluator | medical/hybrid_20iter | `runs_hybrid_20iter` | 2 | 0.5891 (3) | 0.7222 / 0.0588 / 0.1333 |
| 02_hybrid_2026-09-18 | DeepSeek agents on OpenRouter + local evaluator | medical/hybrid_v2 | `runs_hybrid_v2` | 5 | 0.6111 (4) | 0.6111 / 0.0000 / 0.0000 |
| 03_full579_2026-09-18 | full 579-checkpoint medical runs, v4 = 20 iterations | medical/full579_20iter | `runs_full579_20iter` | 1 | 0.3195 (1) | 0.3714 / 0.0938 / 0.0508 |
| 03_full579_2026-09-18 | full 579-checkpoint medical runs, v4 = 20 iterations | medical/full579_v2 | `runs_full579_v2` | 1 | - | - |
| 03_full579_2026-09-18 | full 579-checkpoint medical runs, v4 = 20 iterations | medical/full579_v3 | `runs_full579_v3` | 1 | - | - |
| 03_full579_2026-09-18 | full 579-checkpoint medical runs, v4 = 20 iterations | medical/full579_v4 | `runs_full579_v4` | 20 | 0.5869 (17) | 0.6476 / 0.0885 / 0.0056 |
| 04_fresh40_2026-09-19 | fresh 40-checkpoint dev slice | medical | `runs_fresh40` | 24 | 0.6314 (22) | 0.7048 / 0.0833 / 0.0226 |
| 05_v2_20iter_2026-09-20 | 20-iteration medical, v2 pipeline | medical | `runs_v2_20iter` | 19 | 0.4128 (16) | 0.4286 / 0.0312 / 0.0056 |
| 06_v3_20iter_2026-09-21 | 20-iteration medical, v3 pipeline | medical | `runs_v3_20iter` | 20 | 0.7026 (18) | 0.7667 / 0.0677 / 0.0169 |
| 07_office_first_2026-09-21 | first office runs (domain-specific template, then seeded) | office/office_template | `runs_office_template` | 1 | - | - |
| 07_office_first_2026-09-21 | first office runs (domain-specific template, then seeded) | office/office_seeded | `runs_office_seeded` | 19 | 0.4906 (10) | 0.5714 / 0.0702 / 0.0766 |
| 08_generic_2026-09-21 | generic template, both domains (medical did not work) | medical | `runs_medical_generic` | 20 | 0.1350 (15) | 0.1619 / 0.1615 / 0.0056 |
| 08_generic_2026-09-21 | generic template, both domains (medical did not work) | office | `runs_office_generic` | 19 | 0.5455 (16) | 0.6039 / 0.0585 / 0.0405 |
| 09_generic_v2_2026-09-22 | denial_saturation evidence for the Critic; office v3-v5 = relaunch attempts | medical | `runs_medical_generic_v2` | 20 | 0.5578 (10) | 0.5667 / 0.0156 / 0.0000 |
| 09_generic_v2_2026-09-22 | denial_saturation evidence for the Critic; office v3-v5 = relaunch attempts | office | `runs_office_generic_v2` | 20 | 0.2580 (20) | 0.2662 / 0.0175 / 0.0135 |
| 09_generic_v2_2026-09-22 | denial_saturation evidence for the Critic; office v3-v5 = relaunch attempts | office_v3 | `runs_office_generic_v3` | 5 | - | - |
| 09_generic_v2_2026-09-22 | denial_saturation evidence for the Critic; office v3-v5 = relaunch attempts | office_v4 | `runs_office_generic_v4` | 10 | 0.2627 (10) | 0.2727 / 0.0234 / 0.0135 |
| 09_generic_v2_2026-09-22 | denial_saturation evidence for the Critic; office v3-v5 = relaunch attempts | office_v5 | `runs_office_generic_v5` | 4 | - | - |
| 10_generic_rerun_2026-09-22 | neutral RBAC tests; migrations persisted | medical | `runs_medical_generic_rerun` | 15 | 0.4850 (7) | 0.6143 / 0.1875 / 0.0282 |
| 10_generic_rerun_2026-09-22 | neutral RBAC tests; migrations persisted | office | `runs_office_generic_rerun` | 11 | 0.0000 (10) | 0.0000 / 0.0058 / 0.0000 |
| 11_generic_rerun2_2026-09-23 | same, clean relaunch | medical | `runs_medical_generic_rerun2` | 19 | 0.4592 (16) | 0.4667 / 0.0104 / 0.0056 |
| 11_generic_rerun2_2026-09-23 | same, clean relaunch | office | `runs_office_generic_rerun2` | 20 | 0.5270 (14) | 0.6364 / 0.1287 / 0.0495 |
| 12_generic_fixed_2026-09-23 | pipeline fixes 1-4 (parse all calls, whole-file reads, contract tests, notebook) - aborted for architect budget | medical | `runs_medical_generic_fixed` | 3 | 0.4494 (2) | 0.5381 / 0.1406 / 0.0282 |
| 12_generic_fixed_2026-09-23 | pipeline fixes 1-4 (parse all calls, whole-file reads, contract tests, notebook) - aborted for architect budget | office | `runs_office_generic_fixed` | 6 | 0.2434 (4) | 0.2468 / 0.0000 / 0.0135 |
| 12_generic_fixed_2026-09-23 | pipeline fixes 1-4 (parse all calls, whole-file reads, contract tests, notebook) - aborted for architect budget | smoke | `runs_smoke_fixes` | 2 | 0.5000 (1) | 0.5000 / 0.0000 / 0.0000 |
| 13_generic_fixed2_2026-09-23 | fixes + architect reasoning budget + probe + resume; the artifact's results | medical | `runs_medical_generic_fixed2` | 21 | 0.5683 (20) | 0.6810 / 0.1510 / 0.0169 |
| 13_generic_fixed2_2026-09-23 | fixes + architect reasoning budget + probe + resume; the artifact's results | office | `runs_office_generic_fixed2` | 22 | 0.4112 (19) | 0.4545 / 0.0702 / 0.0270 |
| 14_generic_keyed_2026-09-24 | per-principal keyring, first version (unconditional grants) | medical | `runs_medical_generic_keyed` | 22 | 0.4710 (10) | 0.5714 / 0.1615 / 0.0169 |
| 14_generic_keyed_2026-09-24 | per-principal keyring, first version (unconditional grants) | office | `runs_office_generic_keyed` | 20 | 0.5558 (11) | 0.6753 / 0.1462 / 0.0360 |
| 14_generic_keyed_2026-09-24 | per-principal keyring, first version (unconditional grants) | smoke | `runs_smoke_keyed` | 2 | 0.5000 (1) | 0.5000 / 0.0000 / 0.0000 |
| 15_generic_keyed2_2026-09-24 | frozen grant rule (bound/unbound), probe fixes, recursion limit | medical | `runs_medical_generic_keyed2` | 22 | 0.4968 (10) | 0.6333 / 0.1927 / 0.0282 |
| 15_generic_keyed2_2026-09-24 | frozen grant rule (bound/unbound), probe fixes, recursion limit | office | `runs_office_generic_keyed2` | 22 | 0.5583 (20) | 0.6104 / 0.0643 / 0.0225 |
| 16_generic_think_2026-09-24 | evaluator thinking ON, evidence contract; killed at iter 10/11 for the scoped seed | medical | `runs_medical_generic_think` | 15 | 0.4185 (9) | 0.5429 / 0.2292 / 0.0000 |
| 16_generic_think_2026-09-24 | evaluator thinking ON, evidence contract; killed at iter 10/11 for the scoped seed | office | `runs_office_generic_think` | 16 | 0.5523 (3) | 0.6429 / 0.1170 / 0.0270 |
| 17_generic_scoped_2026-09-24 | scope-aware keyring, authorship grants nothing, evidence contract; thinking ON | medical | `runs_medical_generic_scoped` | 20 | 0.3596 (8) | 0.3714 / 0.0208 / 0.0113 |
| 17_generic_scoped_2026-09-24 | same | office | `runs_office_generic_scoped` | 20 | 0.2937 (18) | - |
| 18_task_template_2026-09-25 | NO seed: task template (TASK.md + stub + frozen keyring + contract tests) + data census; thinking ON | medical | `runs_medical_task_v1` | 20 | 0.8249 (3) | 0.8476 / 0.0156 / 0.0113 |
| 18_task_template_2026-09-25 | same | office | `runs_office_task_v1` | 20 | 0.7508 (12) | 0.8247 / 0.0643 / 0.0270 |
| 19_adaptive_task_2026-09-25 | adaptive task: no census, memory must reorganise within an episode, budgeted memory LLM (llm.py), grants only at write time | medical | `runs_medical_task_adaptive` | 20 | 0.1898 (8) | 0.2333 / 0.1771 / 0.0113 |
| 19_adaptive_task_2026-09-25 | same | office | `runs_office_task_adaptive` | 20 | 0.5379 (20) | 0.7273 / 0.1871 / 0.0901 |
| 20_harnessA_2026-09-28 | adaptive task + harness C0–C11 (DeepInfra pin, near-tie lineage, noise-scaled probe, debug-nesting fix, held-out adaptivity gate, leak/memory-LLM evidence); stopped by user at iter 12 | medical | `runs_medical_task_harnessA` | 12 | 0.4176 (8) | 0.5095 / 0.1615 / 0.0226 |
| 20_harnessA_2026-09-28 | same; office restarted at iters 2, 10, 11 to pick up C6, C10, C11 | office | `runs_office_task_harnessA` | 12 | 0.3023 (5) | 0.3961 / 0.2047 / 0.0405 |
