#!/usr/bin/env python3
"""Combine the 21 per-episode results into one summary.json for this run.

U = utility accuracy, A = privacy (access) leakage rate, F = deletion (forgetting)
leakage rate, OR = over-refusal rate, MGS = U * (1 - A) * (1 - F). The *_strict
variants count forbidden content in the answer model's context as a leak too.
All rates are pooled over checkpoints using the frozen benchmark scorer's own
aggregate(), so they match how each per-episode summary.json was computed.
Read-only on results/; writes summary.json next to this script.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / 'results'
DATASET = Path('/home/thaddus/a-mem-variant/evaluations/v19/v19-model-controls-full-soclaas-20261104T103839Z/frozen-dataset')
ORIGINAL = Path('/home/thaddus/v19-evaluations/medical_domain_original/results')  # same system, hand-written role table
SOCLAAS_EPISODES = {f'_{n:03d}_' for n in range(1, 7)}
EXPECTED = {'checkpoints': 579, 'utility': 210, 'privacy': 192, 'safety': 177, 'episodes': 21}

sys.path.insert(0, str(DATASET))
from bench.eval import scorer  # noqa: E402  (frozen benchmark scorer used by every episode)


def load_scores(path):
    return [json.loads(line) for line in path.open() if line.strip()]


def headline(scores):
    m = scorer.aggregate(scores)
    count = lambda kind, key: sum(s.get(key) is True for s in scores if s['query_type'] == kind)
    return {
        'U': m['utility_accuracy'],
        'A': m['privacy_leakage_rate'],
        'F': m['deletion_leakage_rate'],
        'OR': m['over_refusal_rate'],
        'MGS': m['compliance_utility_score'],
        'A_strict': m['privacy_e2e_leakage_rate'],
        'F_strict': m['deletion_e2e_leakage_rate'],
        'MGS_strict': m['compliance_utility_e2e_score'],
        'action_accuracy': m['action_accuracy'],
        'counts': {
            'checkpoints': m['n_checkpoints'],
            'utility_correct': f"{count('utility', 'utility_correct')}/{m['n_utility_scored']}",
            'privacy_leaks': f"{count('privacy', 'privacy_leak')}/{m['n_privacy_scored']}",
            'deletion_leaks': f"{count('safety', 'deletion_leak')}/{m['n_safety_scored']}",
            'over_refusals': f"{count('utility', 'over_refusal')}/{m['n_utility']}",
            'privacy_strict_leaks': f"{count('privacy', 'privacy_e2e_leak')}/{m['n_privacy_e2e_scored']}",
            'deletion_strict_leaks': f"{count('safety', 'deletion_e2e_leak')}/{m['n_safety_e2e_scored']}",
            'action_correct': f"{sum(s.get('action_correct') is True for s in scores)}/{m['n_checkpoints']}",
        },
    }


episodes, all_scores, soclaas, local = [], [], [], []
for d in sorted(p for p in RESULTS.iterdir() if p.is_dir()):
    summary = json.loads((d / 'summary.json').read_text())
    scores = load_scores(d / 'scores.jsonl')
    all_scores += scores
    (soclaas if d.name in SOCLAAS_EPISODES else local).extend(scores)
    exit_file = d / 'exit-code.txt'
    episodes.append({
        'episode': d.name,
        'backend': 'soclaas' if d.name in SOCLAAS_EPISODES else 'local-vllm',
        'exit_code': int(exit_file.read_text().strip()) if exit_file.exists() else None,
        'complete': summary['complete'],
        'mixed_implementation': summary['mixed_implementation'],
        'recovery_state': summary.get('recovery', {}).get('state'),
        'source_turns': f"{summary['coverage']['ingested_source_turns']}/{summary['coverage']['expected_source_turns']}",
        **headline(scores),
    })

counts = {k: sum(s['query_type'] == k for s in all_scores) for k in ('utility', 'privacy', 'safety')}
ids = [s['checkpoint_id'] for s in all_scores]
run = headline(all_scores)
baseline_scores = [s for d in sorted(p for p in ORIGINAL.iterdir() if p.is_dir()) for s in load_scores(d / 'scores.jsonl')]
baseline = headline(baseline_scores)
def flips(new, old):
    """Per checkpoint, which outcomes changed against the original run."""
    before = {s['checkpoint_id']: s for s in old}
    keys = {'utility': ('utility_correct', True), 'privacy': ('privacy_leak', False), 'safety': ('deletion_leak', False)}
    out = {}
    for s in new:
        b = before.get(s['checkpoint_id'])
        key, good = keys[s['query_type']]
        if b is None or s.get(key) is None or b.get(key) is None or s.get(key) == b.get(key):
            continue
        name = 'fixed' if s.get(key) == good else 'broken'
        out.setdefault(s['query_type'], {}).setdefault(name, []).append(s['checkpoint_id'])
    return {t: {k: {'count': len(v), 'checkpoints': sorted(v)} for k, v in d.items()} for t, d in out.items()}


delta_keys = ('U', 'A', 'F', 'OR', 'MGS', 'A_strict', 'F_strict', 'MGS_strict', 'action_accuracy')

report = {
    'run': ROOT.name,
    'system': 'privacy-aware A-MEM two-store v2, V19 Stage 2 + erasure fix; role tables from a generated policy (role_policy.json) plus learned access (2 source turns) and reply access',
    'code_commit': (ROOT / 'base-commit.txt').read_text().strip(),
    'generated_utc': datetime.now(timezone.utc).isoformat(),
    'metric_definitions': {
        'U': 'utility accuracy over utility checkpoints (higher is better)',
        'A': 'answer-level privacy (access) leakage rate over privacy checkpoints (lower is better)',
        'F': 'answer-level deletion (forgetting) leakage rate over safety checkpoints (lower is better)',
        'OR': 'over-refusal rate over utility checkpoints (lower is better)',
        'MGS': 'U * (1 - A) * (1 - F), the scorer\'s compliance_utility_score (higher is better)',
        'A_strict / F_strict': 'answer leak OR forbidden content in the answer model\'s context',
        'MGS_strict': 'U * (1 - A_strict) * (1 - F_strict), the scorer\'s compliance_utility_e2e_score',
        'pooling': 'rates are pooled over all checkpoints, not averaged across episodes',
    },
    'coverage': {
        'episodes': f"{len(episodes)}/{EXPECTED['episodes']}",
        'checkpoints': f"{len(set(ids))}/{EXPECTED['checkpoints']}",
        'by_type': {k: f"{counts[k]}/{EXPECTED[k]}" for k in counts},
        'duplicate_checkpoints': len(ids) - len(set(ids)),
        'all_episodes_complete': all(e['complete'] for e in episodes),
        'all_exit_codes_zero': all(e['exit_code'] == 0 for e in episodes),
        'any_mixed_implementation': any(e['mixed_implementation'] for e in episodes),
    },
    'overall': run,
    'by_backend': {
        'soclaas (episodes 001-006)': headline(soclaas),
        'local-vllm (episodes 007-021)': headline(local),
    },
    'comparison_to_medical_domain_original': {
        'original_results': str(ORIGINAL),
        'original': baseline,
        'delta_this_run_minus_original': {k: run[k] - baseline[k] for k in delta_keys},
        'checkpoint_changes': flips(all_scores, baseline_scores),
    },
    'per_episode': episodes,
    'scorer': str(DATASET / 'bench/eval/scorer.py'),
    'full_scorer_metrics': scorer.aggregate(all_scores),
}

out = ROOT / 'summary.json'
tmp = out.with_suffix('.json.tmp')
tmp.write_text(json.dumps(report, indent=2) + '\n')
tmp.replace(out)
r = report['overall']
print(f"wrote {out}")
print(f"U={r['U']:.4f}  A={r['A']:.4f}  F={r['F']:.4f}  OR={r['OR']:.4f}  MGS={r['MGS']:.4f}  "
      f"(strict A={r['A_strict']:.4f} F={r['F_strict']:.4f} MGS={r['MGS_strict']:.4f})")
