#!/usr/bin/env python3
"""Pool a run's per-episode results into RUN_DIR/summary.json.

    python pipeline/compile_summary.py runs/<domain> [--baseline DIR | --no-baseline] [--out FILE]

U = utility accuracy, A = privacy (access) leakage rate, F = deletion (forgetting)
leakage rate, OR = over-refusal rate, MGS = U * (1 - A) * (1 - F). The *_strict
variants also count forbidden content in the answer model's context as a leak.
Rates are pooled over checkpoints with the frozen benchmark scorer's aggregate(), so
they match each per-episode summary.json. Read-only on results/.

Baseline: a directory with results/_NNN_/scores.jsonl from an earlier run of the same
domain. The default is references/medical_domain_original for medical and
references/office_domain for office (both used hand-written role tables, written with
test-set visibility, so they are contaminated references); education and household
have none. The report then lists per-checkpoint fixes and breaks against it.

Backends: if RUN_DIR/backends.json (written by pipeline/launch.sh) exists, scores are
also split by backend. The archived medical run predates it; its as-run split was
episodes 001-006 on SOCLAAS and 007-021 on local vLLM (--backend-split medical-as-run).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULT_BASELINES = {'medical': common.REPO / 'references/medical_domain_original',
                     'office': common.REPO / 'references/office_domain'}
AS_RUN_SPLITS = {'medical-as-run': {f'_{n:03d}_': 'soclaas' if n <= 6 else 'local' for n in range(1, 22)}}
DELTA_KEYS = ('U', 'A', 'F', 'OR', 'MGS', 'A_strict', 'F_strict', 'MGS_strict', 'action_accuracy')
METRIC_DEFINITIONS = {
    'U': 'utility accuracy over utility checkpoints (higher is better)',
    'A': 'answer-level privacy (access) leakage rate over privacy checkpoints (lower is better)',
    'F': 'answer-level deletion (forgetting) leakage rate over safety checkpoints (lower is better)',
    'OR': 'over-refusal rate over utility checkpoints (lower is better)',
    'MGS': "U * (1 - A) * (1 - F), the scorer's compliance_utility_score (higher is better)",
    'A_strict / F_strict': "answer leak OR forbidden content in the answer model's context",
    'MGS_strict': "U * (1 - A_strict) * (1 - F_strict), the scorer's compliance_utility_e2e_score",
    'pooling': 'rates are pooled over all checkpoints, not averaged across episodes',
}


def load_scores(path):
    return common.read_jsonl(path)


def flips(new, old):
    """Per checkpoint, which outcomes changed against the baseline run."""
    before = {s['checkpoint_id']: s for s in old}
    keys = {'utility': ('utility_correct', True), 'privacy': ('privacy_leak', False), 'safety': ('deletion_leak', False)}
    out = {}
    for s in new:
        b = before.get(s['checkpoint_id'])
        key, good = keys[s['query_type']]
        if b is None or s.get(key) is None or b.get(key) is None or s.get(key) == b.get(key):
            continue
        out.setdefault(s['query_type'], {}).setdefault('fixed' if s.get(key) == good else 'broken', []).append(s['checkpoint_id'])
    return {t: {k: {'count': len(v), 'checkpoints': sorted(v)} for k, v in d.items()} for t, d in out.items()}


def compile_run(run_dir, baseline=None, backends=None):
    run_dir = Path(run_dir).resolve()
    domain = common.run_domain(run_dir)
    cps = common.checkpoints(domain)
    expected = {'checkpoints': len(cps), 'episodes': len(common.episodes(domain)),
                **{k: sum(c['query_type'] == k for c in cps) for k in ('utility', 'privacy', 'safety')}}
    if backends is None and (run_dir / 'backends.json').is_file():
        backends = json.loads((run_dir / 'backends.json').read_text())
    episodes, all_scores, by_backend = [], [], {}
    for d in common.episode_dirs(run_dir):
        if not (d / 'summary.json').is_file() or not (d / 'scores.jsonl').is_file():
            continue
        summary = json.loads((d / 'summary.json').read_text())
        scores = load_scores(d / 'scores.jsonl')
        all_scores += scores
        backend = (backends or {}).get(d.name)
        if backend:
            by_backend.setdefault(backend, []).extend(scores)
        exit_file = d / 'exit-code.txt'
        episodes.append({
            'episode': d.name, 'backend': backend,
            'exit_code': int(exit_file.read_text().strip()) if exit_file.exists() else None,
            'complete': summary['complete'], 'mixed_implementation': summary['mixed_implementation'],
            'recovery_state': summary.get('recovery', {}).get('state'),
            'source_turns': f"{summary['coverage']['ingested_source_turns']}/{summary['coverage']['expected_source_turns']}",
            **common.headline(scores)})
    if not all_scores:
        raise SystemExit(f'{run_dir}: no finished episode results yet')
    counts = {k: sum(s['query_type'] == k for s in all_scores) for k in ('utility', 'privacy', 'safety')}
    ids = [s['checkpoint_id'] for s in all_scores]
    run = common.headline(all_scores)
    policy = run_dir / 'role_policy.json'
    report = {
        'run': run_dir.name, 'domain': domain,
        'system': 'EvolvingPrivacyMem: privacy-aware A-MEM two-store v2, V19 Stage 2 + erasure fix; role tables from a '
                  'generated policy (role_policy.json) plus learned access (2 source turns) and reply access',
        'role_policy_sha256': common.sha256(policy) if policy.is_file() else None,
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'metric_definitions': METRIC_DEFINITIONS,
        'coverage': {
            'episodes': f"{len(episodes)}/{expected['episodes']}",
            'checkpoints': f"{len(set(ids))}/{expected['checkpoints']}",
            'by_type': {k: f"{counts[k]}/{expected[k]}" for k in counts},
            'duplicate_checkpoints': len(ids) - len(set(ids)),
            'all_episodes_complete': all(e['complete'] for e in episodes),
            'all_exit_codes_zero': all(e['exit_code'] == 0 for e in episodes),
            'any_mixed_implementation': any(e['mixed_implementation'] for e in episodes),
        },
        'overall': run,
        'by_backend': {name: common.headline(scores) for name, scores in sorted(by_backend.items())},
    }
    if baseline:
        baseline = Path(baseline).resolve()
        base_scores = [s for d in common.episode_dirs(baseline) for s in load_scores(d / 'scores.jsonl')]
        base = common.headline(base_scores)
        report['comparison_to_baseline'] = {
            'baseline_results': str(baseline.relative_to(common.REPO)) if baseline.is_relative_to(common.REPO) else str(baseline),
            'baseline': base,
            'delta_this_run_minus_baseline': {k: run[k] - base[k] for k in DELTA_KEYS},
            'checkpoint_changes': flips(all_scores, base_scores)}
    report['per_episode'] = episodes
    report['scorer'] = 'data/gatemem/bench/eval/scorer.py'
    report['full_scorer_metrics'] = common.bench_scorer().aggregate(all_scores)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir', type=Path)
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--baseline', type=Path)
    group.add_argument('--no-baseline', action='store_true')
    ap.add_argument('--backend-split', choices=sorted(AS_RUN_SPLITS), help='as-run backend split for runs without backends.json')
    ap.add_argument('--out', type=Path, help='default: RUN_DIR/summary.json')
    args = ap.parse_args(argv)
    domain = common.run_domain(args.run_dir)
    baseline = None if args.no_baseline else (args.baseline or DEFAULT_BASELINES.get(domain))
    report = compile_run(args.run_dir, baseline, AS_RUN_SPLITS.get(args.backend_split))
    out = args.out or args.run_dir / 'summary.json'
    common.write_json(out, report)
    r = report['overall']
    print(f'wrote {out}')
    print(f"{domain}: U={r['U']:.4f}  A={r['A']:.4f}  F={r['F']:.4f}  OR={r['OR']:.4f}  MGS={r['MGS']:.4f}  "
          f"(strict A={r['A_strict']:.4f} F={r['F_strict']:.4f} MGS={r['MGS_strict']:.4f})  coverage {report['coverage']['checkpoints']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
