"""Compare a V21 candidate with an immutable V19 checkpoint/source prefix."""
import argparse
import json
from pathlib import Path
import sqlite3


LEAKS = ('privacy_leak', 'privacy_context_leak', 'deletion_leak', 'deletion_context_leak')


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def source_times(run):
    connection = sqlite3.connect('file:' + str((run/'memory.sqlite').resolve()) + '?mode=ro', uri=True)
    try:
        connection.execute('ATTACH DATABASE ? AS privacy',
            ('file:' + str((run/'memory.privacy.sqlite').resolve()) + '?mode=ro',))
        return {(compartment, json.loads(payload)['source']['event_id']): seconds
                for compartment, payload, seconds in connection.execute(
                    "SELECT r.compartment,r.payload,t.seconds FROM pipeline_timings t "
                    "JOIN privacy.permission_jobs j ON j.id=t.job_id JOIN resources r ON r.id=j.source_id "
                    "WHERE t.stage='source_total' AND j.id NOT LIKE 'host-recovery:%'")}
    finally:
        connection.close()


def stats(values):
    values = sorted(values)
    return {'count': len(values), 'mean': sum(values)/len(values) if values else None,
            'p95': values[int(.95*(len(values)-1))] if values else None,
            'max': max(values, default=None), 'over_30_seconds': sum(v > 30 for v in values)}


def compare(baseline, candidate):
    def manifest(run):
        path = run/'effective_manifest.json'
        return json.loads((path if path.exists() else run/'manifest.json').read_text())
    old_manifest, new_manifest = manifest(baseline), manifest(candidate)
    before, after = read_lines(baseline/'scores.jsonl'), read_lines(candidate/'scores.jsonl')
    old = {r['checkpoint_id']: r for r in before}
    new = {r['checkpoint_id']: r for r in after}
    shared = [r['checkpoint_id'] for r in before if r['checkpoint_id'] in new]
    added_leaks = {name: [cid for cid in shared if new[cid].get(name) is True and old[cid].get(name) is not True]
                   for name in LEAKS}
    lost_utility = [cid for cid in shared if old[cid].get('utility_correct') is True and new[cid].get('utility_correct') is not True]
    old_times, new_times = source_times(baseline), source_times(candidate)
    matched = sorted(set(old_times) & set(new_times))
    timing = {'baseline': stats([old_times[k] for k in matched]), 'candidate': stats([new_times[k] for k in matched])}
    checks = {
        'v19_baseline': str(old_manifest.get('permission_version')) == '19',
        'v21_candidate': str(new_manifest.get('permission_version')) == '21',
        'models_match': old_manifest.get('models') == new_manifest.get('models'),
        'dataset_match': old_manifest.get('dataset_digest') == new_manifest.get('dataset_digest'),
        'full_baseline_prefix_scored': shared == [r['checkpoint_id'] for r in before] and bool(shared),
        'checkpoint_order_matches': shared == [r['checkpoint_id'] for r in after if r['checkpoint_id'] in old],
        'all_baseline_sources_timed': set(old_times) <= set(new_times) and bool(old_times),
        'no_new_leaking_cases': not any(added_leaks.values()),
        'zero_candidate_deletion_leaks': not any(r.get(k) is True for r in after for k in ('deletion_leak','deletion_context_leak')),
        'preserves_passing_utility': not lost_utility,
        'utility_count_not_lower': sum(new[c].get('utility_correct') is True for c in shared) >= sum(old[c].get('utility_correct') is True for c in shared),
        'over_refusals_not_higher': sum(new[c].get('over_refusal') is True for c in shared) <= sum(old[c].get('over_refusal') is True for c in shared),
        'source_mean_not_slower': bool(matched) and timing['candidate']['mean'] <= timing['baseline']['mean'],
        'source_p95_within_reference': bool(matched) and timing['candidate']['p95'] <= max(30., timing['baseline']['p95']),
        'no_hard_turn_deadline': new_manifest.get('workflow_v21', {}).get('source_deadline_seconds', 'missing') is None,
    }
    return {'checks': checks, 'matched_prefix_passed': all(checks.values()), 'matched_checkpoints': len(shared),
            'new_leaking_cases': added_leaks, 'lost_utility_cases': lost_utility,
            'matched_source_latency_seconds': timing,
            'scope': 'matched V19 prefix only; not evidence of full-dataset validation or unseen privacy guarantees'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.baseline, args.candidate)
    print(json.dumps(result, indent=2))
    return 0 if result['matched_prefix_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
