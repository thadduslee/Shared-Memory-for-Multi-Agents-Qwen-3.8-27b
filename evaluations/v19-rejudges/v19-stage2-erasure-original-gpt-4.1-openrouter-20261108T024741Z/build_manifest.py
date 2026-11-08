#!/usr/bin/env python3
"""Write manifest.json for this rejudge: pins inputs, frozen code/fixtures and the source run's files by SHA-256."""
from datetime import datetime, timezone
import hashlib, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path('/home/thaddus/v19-evaluations/v19-stage2-erasure-full-20261107T155701Z')
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
episodes = sorted(d for d in (SRC / 'results').iterdir() if d.is_dir())
original = {}
for d in episodes:
    for name in ('official_predictions.jsonl', 'scores.jsonl', 'summary.json', 'manifest.json'):
        original[str(d / name)] = sha(d / name)
original[str(SRC / 'summary.json')] = sha(SRC / 'summary.json')
files = sorted(p for top in ('inputs', 'frozen-source', 'frozen-dataset') for p in (HERE / top).rglob('*')
               if p.is_file() and '__pycache__' not in p.parts)
manifest = {
    'source': {
        'run_dir': str(SRC),
        'code_commit': (SRC / 'base-commit.txt').read_text().strip(),
        'episodes': {d.name: {'manifest_sha256': sha(d / 'manifest.json'),
                              'official_predictions_sha256': sha(d / 'official_predictions.jsonl')} for d in episodes},
        'combined_official_predictions_sha256': sha(HERE / 'inputs/official_predictions.jsonl'),
        'combination': 'per-episode official_predictions.jsonl and scores.jsonl concatenated in episode order _001_.._021_',
    },
    'judge': {
        'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'openai/gpt-4.1',
        'temperature': 0, 'max_tokens': 2048, 'workers': 6, 'semantic_context_review': True,
        'provider_preferences': {'require_parameters': True, 'sort': 'latency'},
    },
    'scoring_sha256': sha(HERE / 'frozen-source/evaluation/gatemem_medical/scoring.py'),
    'created_utc': datetime.now(timezone.utc).isoformat(),
    'scoring_policy': ('Unmodified original frozen V19 GateMemScorer (scoring.py, rejudge.py and processors.py byte-identical '
                       'to the original V19 GPT-4.1 rejudge), original predictions and retrieved_memory. Full fresh LLM answer '
                       'verdicts, original auxiliary action/context scoring and semantic context review. No govmem strict rejudge.'),
    'input_sha256': {str(p.relative_to(HERE)): sha(p) for p in files},
    'original_files_sha256': original,
    'runner_sha256': sha(HERE / 'run_rejudge.py'),
}
(HERE / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(len(manifest['input_sha256']), 'pinned files;', len(original), 'source files')
