# Governing Shared Memory for Multi-Agent Systems

Research overview · Revised 9 October 2026

This report examines the motivation, research question, and emerging evidence behind the shared-memory systems in this repository, especially `Auto Research/runs_ours` and `gsm-rebuild`. The industry scenarios below illustrate potential applications; the recorded experiments evaluate synthetic benchmark conversations, rather than deployments in those industries.

## 1. Introduction and motivation

### Shared memory as a foundation for collaboration

AI agents become more useful when they can carry knowledge from one interaction to the next. A persistent memory can preserve decisions, commitments, preferences, and the reasons behind earlier actions. When several agents support the same organization, sharing that memory could reduce repeated explanations, connect work across departments, and preserve continuity when people or agents hand tasks over. A planning agent, a scheduling agent, and a support agent could coordinate around a common history instead of maintaining incompatible accounts of the same situation.

Sharing also changes the nature of the problem. A private assistant mainly needs to remember what matters to one user. A collaborative system must distinguish what different participants may know. An employee may contribute information to a shared workflow without authorizing every colleague, contractor, or automated tool to see it. A requester may be entitled to a summary while lacking permission to inspect its underlying details. Information can remain useful to an organization after one participant's access has ended, while another item must be deleted altogether.

The research therefore treats memory as an evolving collection of facts, permissions, and obligations. Its value depends on retrieving the right information for the right requester at the right time. This becomes especially significant when agents act across organizational boundaries: a shared memory can support coordination, but an inappropriate transfer of context can undermine the trust that makes coordination possible.

### Corporate collaboration and business operations

Consider an organization using agents for project management, finance, human resources, and customer support. These workflows share operational context, but their information needs differ. A project agent may need staffing availability without receiving private personnel assessments. A finance agent may need approved spending commitments without unrestricted access to confidential negotiations. A contractor may need the technical requirements of an assignment while remaining outside unrelated customer records or internal strategy discussions.

Governed memory could support these exchanges through selective disclosure. Instead of copying an entire meeting transcript into every agent's context, the system could release a project deadline, an approved budget summary, or a relevant task dependency under the requester's permissions. Provenance would help connect an answer to the decision or instruction that supports it. Versioning would distinguish the current deadline from an earlier proposal, reducing the risk that an agent acts on an outdated commitment.

The same logic applies as teams change. A reassignment can grant access to existing project knowledge; the end of a contract can revoke it. A corrected financial assumption should replace the previous value in current answers while preserving separately authorized historical access. The potential benefit is organizational continuity with explicit boundaries: shared knowledge remains available to legitimate collaborators as membership and responsibilities evolve.

### Medical care and coordination

Medical coordination provides a particularly clear example of information serving several legitimate purposes at different levels of detail. A patient, clinician, scheduling assistant, caregiver, and family member may all participate in a care episode. A scheduling assistant may need appointment times and locations. A family member may be permitted to help with transport without receiving clinical findings. A treating clinician may need a fuller history, while the patient's sharing preferences can change during the episode.

A governed memory could preserve continuity across these interactions without treating participation as unrestricted access. Permission to coordinate transport would authorize logistical evidence, and permission to discuss a particular result would apply only to the relevant information. If consent is withdrawn, the change must affect facts already stored, including summaries derived from them. A correction to a medication or appointment should also change what the system presents as current.

This setting illustrates why useful memory and appropriate disclosure must be designed together. Excessive restriction can prevent a legitimate user from receiving needed information; excessive sharing can reveal details outside the user's role or consent. The research investigates that tension through synthetic conversations. It does not establish suitability for clinical decision-making or compliance with any medical regulatory framework.

### Defense and coordination across security boundaries

Defense settings suggest another potential application: coordination among teams with different responsibilities, clearances, compartments, and partner relationships. An agent supporting logistics may need an approved delivery requirement without access to the sensitive assessment behind it. A planning assistant may need a sanitized constraint derived from several sources, while access to those sources remains restricted. Collaboration with an external partner can introduce additional boundaries even when participants work toward a shared objective.

Such a system would need to govern derived information as carefully as source records. A summary should not gain broader access merely because it uses different wording. Its permissions must reflect the restrictions of the information from which it was produced. Changes in assignment or sharing authority should also propagate to existing memory, rather than applying only to future messages.

The relevance of this research is the separation between language interpretation and authorization. A model could interpret a request or propose a summary, while authenticated identity and explicit policy determine which evidence it receives. Provenance, controlled projections, and auditable decisions could support accountability across organizational boundaries. Defense is a motivating scenario rather than an evaluated domain here; the prototype makes no claim of clearance-system integration, formal information-flow security, or operational readiness.

### Other settings with the same underlying structure

The problem extends to education, financial services, legal work, and industrial collaboration. In education, a scheduling agent might coordinate accommodations while counseling notes remain restricted. In financial services, relationship managers, analysts, and external advisers could share selected account context under different permissions. Legal teams could preserve matter-specific knowledge while separating unrelated clients and controlling access as team membership changes. Manufacturers and suppliers could coordinate around approved specifications without disclosing unrelated designs or commercial terms.

These scenarios differ in vocabulary and institutional policy, but share a common structure: multiple participants, overlapping information needs, changing authority, and facts that can be corrected or withdrawn. A broadly useful memory mechanism should represent that structure without relying on a fixed list of familiar roles. The ambition is a reusable governance mechanism whose policies can be configured for a setting and whose state can adapt as interactions unfold.

### Why ordinary retrieval and prompt instructions are insufficient

A conventional retrieval objective asks which records are most relevant to a question. Shared-memory governance adds an earlier question: which records, or parts of records, may this requester receive? A highly relevant fact may still be inaccessible. Ranking the entire store and asking the answer model to exercise restraint means the protected content has already entered that model's context. Low leakage in the final response can therefore conceal a failure at the memory boundary.

Lifecycle changes make this harder. An access rule can change after a fact is stored. A correction can make a previous value outdated without requiring its deletion. A deletion can require removal from retrieval even if copies remain in summaries, indexes, or caches. The system must distinguish current truth, permitted historical access, revoked disclosure, and deleted content. Otherwise, a later search or reconstruction may restore information that should no longer be available.

Granularity matters as well. A single paragraph may contain both shareable logistics and protected personal details. Blocking the whole paragraph loses useful information; releasing it whole exceeds the requester's scope. Splitting it into independently governable facts helps selective disclosure, but can lose contextual relationships. The research must address how to preserve enough meaning for useful answers while maintaining precise access and lifecycle boundaries.

### From motivation to a research question

These requirements lead to the central question:

> **How can a persistent shared-memory system remain useful to multiple agents and people while enforcing requester-specific access, adapting to changes in consent and facts, and preventing the recovery of deleted information across both source and derived memories?**

Three connected questions make this objective concrete. First, how can natural-language interactions become structured facts and policy changes without giving the interpreting model unrestricted authority? Second, how can retrieval preserve relevant context while ensuring that only authorized projections reach the answerer? Third, how should permission changes, supersession, and deletion propagate through existing and derived memory as the store grows?

The working hypothesis is that reliable governance requires explicit memory structure and mandatory enforcement. Models can interpret dialogue and formulate responses, while programmatic layers decide authorization, maintain lifecycle state, and verify releases. Success requires useful answers as well as controlled information flow. A system that refuses everything fails the collaboration objective, while one that produces restrained answers after retrieving forbidden evidence fails the memory-governance objective.

The repository pursues this through two related efforts. Auto Research iteratively proposes, implements, evaluates, and critiques memory mechanisms; its later tasks require within-episode adaptation to unfamiliar roles and language. Governed Shared Memory (GSM) implements a service architecture with versioned memory atoms, provenance, trusted requester context, deterministic policy decisions, authorized retrieval, and controlled disclosure. Deletion uses tombstones and cascades through live content and derived memories. Together, these efforts investigate both how to discover better mechanisms and how to make governance an enforced property of the memory service. [Adaptive research task](<Auto Research/templates_task/TASK.md>), [GSM architecture](gsm-rebuild/docs/ARCHITECTURE.md).

## 2. Preliminary findings and problems faced

The baseline analysis supports the distinction between answer restraint and memory governance. Some methods produced few final-answer leaks while supplying protected or deleted records to the answerer very frequently. This motivates authorization before retrieval and generation. However, filtering must preserve the surrounding context that makes legitimate requests understandable: an answerer can refuse even when the relevant fact is present if requester identity, standing instructions, or speaker attribution are missing. [Baseline analysis](docs/RESULTS.md).

A second finding is that deterministic enforcement depends on the quality of interpreted policy. GSM can consistently apply an audience restriction, but it cannot make an incorrectly extracted audience correct. Overly broad interpretation exposes information; overly narrow interpretation prevents useful answers. Consent and revocation also require resolving which earlier facts an instruction covers. References by name, partial corrections, and mixed-content deletion requests remain difficult.

Auto Research exposed a related generalization problem. Earlier high-scoring implementations used dataset-informed role and relationship rules. Later experiments required unfamiliar vocabulary, no data census, and permission changes during ingestion, substantially increasing the difficulty. Synthetic adaptivity checks helped reveal shortcuts that ordinary contract tests missed. These checks test specific mechanisms, rather than establishing general performance on unseen benchmark episodes. [Experiment history](<Auto Research/runs_ours/README.md>).

The automated loop also needed dependable feedback. Missing retrieval diagnostics, provider failures, limited model budgets, and noisy candidate selection could obscure the actual design problem. Score histories, failure classification, persistent critique notes, and better coverage evidence made the loop more informative. These changes were introduced together, so their individual effects have not been isolated. [Harness findings](<Auto Research/runs_ours/NOTES_harness_2026-09-27.md>).

Finally, useful governance must survive growth. Supplying a small authorized memory in full can preserve context, but a large store requires selective retrieval that may omit essential evidence. Deletion also needs a clearly stated boundary: GSM blocks access and scrubs live service-owned content, but does not certify physical erasure from backups, database internals, or provider logs. The benchmark adapter confirms speaker-requested changes; production model ingestion quarantines high-impact proposals. [Security boundary](gsm-rebuild/docs/SECURITY.md).

## 3. Results

### Experimental setup and measurement

GateMem provides 91 synthetic episodes across medical, office, education, and household settings, with 2,218 checkpoints in total. Conversations are replayed chronologically, and methods receive visible requester and episode information while scoring annotations remain withheld. Utility **U**, access-control violations **A**, and forgetting failures **F** are combined as **MGS = U × (1 − A) × (1 − F)**. Answer-level MGS measures final responses. Strict MGS additionally counts protected information in the answerer's prompt as leakage. [Evaluation protocol](docs/evaluation_protocol.md), [scorer](bench/eval/scorer.py).

The later GSM comparison uses the same benchmark Qwen3.8-27B answerer and Qwen3.8-27B judge across seven baselines and three GSM configurations. Strict scores combine the judge's answer verdicts with the official scorer's pattern checks for context exposure. Each system covers all 2,218 checkpoints; reported means weight the four domains equally. These results use different settings from earlier GPT-4.1-judged baseline analyses and Auto Research development runs.

GSM v13 represents the original governed pipeline. V20auth adds repairs that preserve legitimate readers across updates, supply scoped evidence, retain unchanged details, and avoid deleting an entire mixed-content record. V22hist adds the requester's own activity context and deletion across earlier versions. In these runs, authorized evidence is supplied in full when it fits 400 records and 60,000 characters; larger sets use ranked top-20 retrieval. Because repairs also change later interpretation inputs, the version comparisons measure combined system behavior rather than isolated component effects.

### Development trajectory and four-domain comparison

Auto Research's best development MGS values illustrate the effect of changing task constraints:

| Experiment | Medical | Office |
|---|---:|---:|
| Try 18: task template with data census | 0.825 | 0.751 |
| Try 19: stricter adaptive task without census | 0.190 | 0.538 |
| Try 20: adaptive harness A | 0.418 | 0.302 |

These are selected development results, rather than controlled comparisons or held-out test scores. Try 18 benefited from dataset-informed construction; later tasks pursued a more general mechanism. Harness A improved medical performance within its run, while office remained difficult. Both latest runs stopped with iteration 12 incomplete. [Run inventory](<Auto Research/runs_ours/README.md>), [medical champion](<Auto Research/runs_ours/20_harnessA_2026-09-28/medical/iter_8/dev/judge_report.json>), [office champion](<Auto Research/runs_ours/20_harnessA_2026-09-28/office/iter_5/dev/judge_report.json>).

The later GSM evaluation shows the main governance trade-off:

| System | Mean answer-level MGS | Mean strict MGS |
|---|---:|---:|
| Long-context | **0.624** | 0.000 |
| Naive RAG | 0.374 | 0.003 |
| Policy RAG | 0.325 | 0.075 |
| A-Mem | 0.385 | 0.003 |
| Mem0 | 0.288 | 0.013 |
| ReMeM iterative | 0.297 | 0.005 |
| ReMeM single | 0.222 | 0.010 |
| GSM v13 | 0.505 | **0.315** |
| GSM v20auth | 0.538 | 0.299 |
| GSM v22hist | 0.539 | 0.290 |

Long-context leads mean answer-level performance. GSM achieves the strongest strict score in every domain when the best configuration is compared with the baselines. V13's mean strict MGS is approximately **4.2 times** that of policy RAG, the strongest baseline on this measure. Later repairs improve answer-level MGS while slightly reducing strict MGS, showing that wider useful evidence can also increase exposure. V13 still exposes forbidden content in approximately 18–37% of privacy prompts and 9–26% of deletion prompts. [Four-domain findings](gsm-rebuild/results/four_domain_scores.md), [saved aggregates](gsm-rebuild/outputs/gatemem/strict_scores/strict_scores.json).

### Persistent memory and interpretation of the evidence

With one persistent memory per domain and no episode resets, GSM v13 reaches mean answer-level/strict MGS of **0.448/0.249**, compared with long-context's **0.645/approximately zero**. GSM's office utility falls from 0.747 to 0.429 as recurring users accumulate more readable records than its full-context budget allows. Ranked retrieval then misses required evidence, illustrating the importance of scaling governed recall alongside access enforcement. [Continuous evaluation](gsm-rebuild/results/continuous_setting.md).

The results support enforced governance before generation, but do not establish solved access control, forgetting, or industry readiness. The experiments use synthetic development data without a separate final held-out evaluation or reported confidence intervals. Pattern checks and model judging introduce uncertainty. The next research step is to test whether more reliable interpretation, lifecycle propagation, and scalable evidence selection can preserve these governance gains on unfamiliar, persistent collaborative workflows.
