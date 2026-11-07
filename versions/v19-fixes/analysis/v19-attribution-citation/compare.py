"""Score two runs on the checkpoints both completed, so partial runs compare fairly.

Usage: python analysis/v19-attribution-citation/compare.py BASELINE_DIR NEW_DIR
"""
import json
from pathlib import Path
import sys


def rows(run):
    return {r['checkpoint_id']: r for r in map(json.loads, (Path(run) / 'results/scores.jsonl').open())}


def rate(items, key):
    values = [bool(r[key]) for r in items if r.get(key) is not None]
    return f'{sum(values)}/{len(values)}' + (f' ({sum(values) / len(values):.1%})' if values else '')


def report(name, items):
    utility = [r for r in items if r['query_type'] == 'utility']
    print(f'{name}: action {rate(items, "action_correct")}; over-refusal {rate(utility, "over_refusal")}; '
          f'utility {rate(utility, "utility_correct")}; privacy leak {rate(items, "privacy_leak")}; '
          f'privacy context {rate(items, "privacy_context_leak")}; deletion leak {rate(items, "deletion_leak")}; '
          f'deletion context {rate(items, "deletion_context_leak")}')


def seconds_per_turn(run):
    summary = json.loads((Path(run) / 'results/summary.json').read_text())
    turns = summary['coverage']['ingested_source_turns']
    return summary['pipeline_seconds']['source_total'] / turns if turns else None, summary


baseline, new = rows(sys.argv[1]), rows(sys.argv[2])
shared = sorted(baseline.keys() & new.keys())
print(f'{len(shared)} shared checkpoints')
report('baseline', [baseline[c] for c in shared])
report('new     ', [new[c] for c in shared])
print('New leaks to read by hand:')
for checkpoint in shared:
    if any(new[checkpoint].get(k) and not baseline[checkpoint].get(k) for k in
            ('privacy_leak', 'privacy_context_leak', 'deletion_leak', 'deletion_context_leak')):
        print(' ', checkpoint, new[checkpoint].get('attack_type'), '|', new[checkpoint]['judge'].get('notes'))
for name, run in (('baseline', sys.argv[1]), ('new', sys.argv[2])):
    per_turn, summary = seconds_per_turn(run)
    processing = summary['memory_processing']
    inference = summary['permission_inference']
    print(f'{name}: {per_turn:.1f}s per turn; {inference["semantic_calls_per_source"]} calls per turn; '
          f'candidates {processing["candidate_states"]}')
