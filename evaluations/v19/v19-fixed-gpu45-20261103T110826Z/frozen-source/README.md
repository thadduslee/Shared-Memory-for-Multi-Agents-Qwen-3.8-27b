# Agentic Memory 🧠

A novel agentic memory system for LLM agents that can dynamically organize memories in an agentic way.

The raw-stream privacy runtime defaults to version 19. Its explicit, opt-in OWNS
policy permits personal owners to read their own attributed facts across tasks;
other readers need separate scoped grants. The medical adapter and standalone
example enable this policy. General integrations configure `personal_owners`
and `owner_read_actions`, or attest personal resources through the existing
authenticated discovery connector. Administrator authority never implies ownership.
Restrictions, revocation, forgetting and final answer authorization still apply.
See the [ownership policy and validation status](analysis/incremental-v17/V19.md).
Historical inference versions remain explicitly selectable; use a fresh store
for the new contract. Existing evaluation artifacts are preserved.

## Introduction 🌟

Large Language Model (LLM) agents have demonstrated remarkable capabilities in handling complex real-world tasks through external tool usage. However, to effectively leverage historical experiences, they require sophisticated memory systems. Traditional memory systems, while providing basic storage and retrieval functionality, often lack advanced memory organization capabilities.

Our project introduces an innovative **Agentic Memory** system that revolutionizes how LLM agents manage and utilize their memories:

<div align="center">
  <img src="Figure/intro-a.jpg" alt="Traditional Memory System" width="600"/>
  <img src="Figure/intro-b.jpg" alt="Our Proposed Agentic Memory" width="600"/>
  <br>
  <em>Comparison between traditional memory system (top) and our proposed agentic memory (bottom). Our system enables dynamic memory operations and flexible agent-memory interactions.</em>
</div>

> **Note:** This repository provides a memory system to facilitate agent construction. If you want to reproduce the results presented in our paper, please refer to: [https://github.com/WujiangXu/AgenticMemory](https://github.com/WujiangXu/AgenticMemory)

For more details, please refer to our paper: [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/pdf/2502.12110)


## Key Features ✨

- 🔄 Dynamic memory organization based on Zettelkasten principles
- 🔍 Intelligent indexing and linking of memories via ChromaDB
- 📝 Comprehensive note generation with structured attributes
- 🌐 Interconnected knowledge networks
- 🧬 Continuous memory evolution and refinement
- 🤖 Agent-driven decision making for adaptive memory management

## Framework 🏗️

<div align="center">
  <img src="Figure/framework.jpg" alt="Agentic Memory Framework" width="800"/>
  <br>
  <em>The framework of our Agentic Memory system showing the dynamic interaction between LLM agents and memory components.</em>
</div>

## How It Works 🛠️

When a new memory is added to the system:
1. Generates comprehensive notes with structured attributes
2. Creates contextual descriptions and tags
3. Analyzes historical memories for relevant connections
4. Establishes meaningful links based on similarities
5. Enables dynamic memory evolution and updates

## Results 📊

Empirical experiments conducted on six foundation models demonstrate superior performance compared to existing SOTA baselines.

## Getting Started 🚀

1. Clone the repository:
```bash
git clone https://github.com/agiresearch/A-mem.git
cd A-mem
```

2. Install dependencies:
Create and activate a virtual environment (recommended):
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows, use: .venv\Scripts\activate
```

Install the package:
```bash
pip install .
```
For development, you can install it in editable mode:
```bash
pip install -e .
```

3. Usage Examples 💡

Here's how to use the Agentic Memory system for basic operations:

```python
from agentic_memory.memory_system import AgenticMemorySystem

# Initialize the memory system 🚀
memory_system = AgenticMemorySystem(
    model_name='all-MiniLM-L6-v2',  # Embedding model for ChromaDB
    llm_backend="openai",           # LLM backend (openai/ollama)
    llm_model="gpt-4o-mini"         # LLM model name
)

# Add Memories ➕
# Simple addition
memory_id = memory_system.add_note("Deep learning neural networks")

# Addition with metadata
memory_id = memory_system.add_note(
    content="Machine learning project notes",
    tags=["ml", "project"],
    category="Research",
    timestamp="202503021500"  # YYYYMMDDHHmm format
)

# Read (Retrieve) Memories 📖
# Get memory by ID
memory = memory_system.read(memory_id)
print(f"Content: {memory.content}")
print(f"Tags: {memory.tags}")
print(f"Context: {memory.context}")
print(f"Keywords: {memory.keywords}")

# Search memories
results = memory_system.search_agentic("neural networks", k=5)
for result in results:
    print(f"ID: {result['id']}")
    print(f"Content: {result['content']}")
    print(f"Tags: {result['tags']}")
    print("---")

# Update Memories 🔄
memory_system.update(memory_id, content="Updated content about deep learning")

# Delete Memories ❌
memory_system.delete(memory_id)

# Memory Evolution 🧬
# The system automatically evolves memories by:
# 1. Finding semantic relationships using ChromaDB
# 2. Updating metadata and context
# 3. Creating connections between related memories
# This happens automatically when adding or updating memories!
```

### Advanced Features 🌟

1. **ChromaDB Vector Storage** 📦
   - Efficient vector embedding storage and retrieval
   - Fast semantic similarity search
   - Automatic metadata handling
   - Persistent memory storage

2. **Memory Evolution** 🧬
   - Automatically analyzes content relationships
   - Updates tags and context based on related memories
   - Creates semantic connections between memories

3. **Flexible Metadata** 📋
   - Custom tags and categories
   - Automatic keyword extraction
   - Context generation
   - Timestamp tracking

4. **Multiple LLM Backends** 🤖
   - OpenAI (GPT-4, GPT-3.5)
   - Ollama (for local deployment)

### Best Practices 💪

1. **Memory Creation** ✨:
   - Provide clear, specific content
   - Add relevant tags for better organization
   - Let the system handle context and keyword generation

2. **Memory Retrieval** 🔍:
   - Use specific search queries
   - Adjust 'k' parameter based on needed results
   - Consider both exact and semantic matches

3. **Memory Evolution** 🧬:
   - Allow automatic evolution to organize memories
   - Review generated connections periodically
   - Use consistent tagging conventions

4. **Error Handling** ⚠️:
   - Always check return values
   - Handle potential KeyError for non-existent memories
   - Use try-except blocks for LLM operations

## Citation 📚

If you use this code in your research, please cite our work:

```bibtex
@article{xu2025mem,
  title={A-mem: Agentic memory for llm agents},
  author={Xu, Wujiang and Liang, Zujie and Mei, Kai and Gao, Hang and Tan, Juntao and Zhang, Yongfeng},
  journal={arXiv preprint arXiv:2502.12110},
  year={2025}
}
```

## License 📄

This project is licensed under the MIT License. See LICENSE for details.
# Standalone privacy-aware runtime

The new implementation is available as
`agentic_memory.privacy.PrivacyMemorySystem`. It includes canonical policies,
compartment-scoped authorized vector search, protected directed edges,
full-input provenance, restricted enrichment overlays, processor approvals,
revocation/deletion handling, and a standalone incremental GateMem evaluator.
The legacy `AgenticMemorySystem` API remains unguarded; select the new API explicitly.

The runtime now uses separate persistent memory and privacy SQLite vector stores,
with exact conditional access bindings and task-scoped assignments. See
[the implemented architecture and migration guide](PRIVACY_TWO_STORE.md) and
[the medical authority policy](evaluation/gatemem_medical/AUTHORITY_POLICY.md).
The original experiment directories are preserved; new local audits use new paths.
The default GateMem runner now uses [ingestion-time permission inference](PERMISSION_INFERENCE.md),
with a general involvement/restriction ledger and exact field assignments.
The default agent input contains only sequential raw turn text, speaker IDs and
event/time envelopes; `entities`, structured role/access metadata and checkpoint
labels stay with the evaluator. Identities are discovered from observed turns and
no relationship grants are seeded. Start a new run directory for `raw-stream-v1`.
`--permission-mode baseline` retains the metadata-assisted oracle compiler for
comparison; it is not a raw-stream benchmark result.
Version 7 adds [generic capability decisions, recipient coverage, scoped erasure,
and transaction-scoped authorization reuse](GOVERNED_AUTHORIZATION.md). Use a fresh
run directory and `--permission-version 7`; existing v6 artifacts are preserved.
Version 9 adds [incremental permission context and targeted history retrieval](PERMISSION_INFERENCE.md#incremental-context-version-9).
The model receives recent turns, relevant permissions with original evidence and
conditions, pending decisions and all restrictions. Missing evidence is retrieved
before an atomic delta is committed. Start a fresh run with
`--permission-version 9`; earlier run directories retain their original results.
Version 10 adds [final-state validation, durable repair limits, smaller deltas and
deferred enrichment](PERMISSION_INFERENCE.md#per-turn-deltas-and-deferred-enrichment-version-10).
Permissions and fields commit each turn;
optional tags/links are queued and processed after boundary answers. Use a fresh
directory with `--permission-version 10`; prior experiments remain preserved.
Version 15 is the default: [combined incremental permission processing](INCREMENTAL_PERMISSIONS.md).
It starts with empty requester access, persists protected facts independently of
permissions, and batches model reasoning in one call on ordinary turns, with at
most one focused repair/history batch. [Version 14](DURABLE_FACTS.md) remains selectable.
Use a fresh run/store. The retained version 13 uses: [single-pass updates](PERMISSION_INFERENCE.md#single-pass-permission-deltas-version-13)
use the latest six turns and concise current policy, including unconfirmed tasks.
The model reports `permission_changes` and `new_facts` independently in one response;
new facts can bind to existing permissions with no policy edits. Tasks retain the
requester separately from worker and beneficiary, and assess each recipient's access.
Later task facts require separate scope/restriction checks for all active allowed
recipients. Missing allowed facts produce “I don't have that information”; refusals
require a permission assessment. There is no history retrieval or focused
reassessment. Repeated identical overlap IDs are deduplicated before validation;
invalid responses have a durable three-attempt repair budget with validation feedback.
The v13 evaluation records exhausted permission turns as skipped and continues.
Rejected changes stay unpublished; skipped turns retain an access hold on their
episode, and affected checkpoint outputs have `context_complete: false`. The run
summary lists skipped turns and reports degraded permission inference separately
from full boundary/checkpoint coverage.
Original evidence remains in host audit storage; validated exact field bindings
and enforcement are retained.
Use a fresh directory with `--permission-version 13`; old runs are preserved.
Version 12 remains available explicitly and adds [durable task resolution](PERMISSION_INFERENCE.md#durable-task-resolution-version-12)
to the six-turn window and concise current access state. Potential worker tasks
survive until later subject acceptance can resolve them. Affected recipients need
an explicit resolution; supported tasks compile into narrow grants and exact facts.
Reassessment and historical fact lookup are conditional and bounded.
Old authorization text, justifications and binding history remain in host audit
storage and are not fed back into each inference. Current policy is available in
`memory.privacy.sqlite` → `permission_access_state`, or `updater.access_state()`.
Use a fresh directory with `--permission-version 12`; populated legacy permission
stores cannot silently migrate to the new scope contract.
See [the task-resolution validation and live-model limitations](analysis/task-resolution-v12-20261029/REPORT.md).
See [the source assessment](analysis/two-store-implementation/ASSESSMENT.md) and
[the local validation and all 118 dispositions](analysis/two-store-implementation/VALIDATION.md).

See [the standalone runbook](evaluation/gatemem_medical/RUNBOOK.md) for the full
579-query / 567-boundary medical experiment, model-service startup, tmux,
resuming, artifacts, and deployment limits. The new evaluator does not execute
the old Auto Research online system.

The opt-in [v20 workflow](analysis/workflow-v20/README.md) adds bounded host
recovery, independent attribution checks, accepted-service scopes and query
deadlines. V19 remains the default; v20 paid evaluation requires explicit opt-in.

```bash
cd /home/thaddus/a-mem
python3 -m unittest discover -s security_tests -v
python3 -m evaluation.gatemem_medical --plan
bash scripts/run_gatemem.sh --run-dir runs/gatemem-medical-full
```
