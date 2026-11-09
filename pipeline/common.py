"""Shared helpers for the domain pipeline: paths, dataset loading, per-episode results and headline metrics.

A run directory (runs/<domain>/ by default) looks like this:

    role_policy.json(+.sha256, .provenance.json)  frozen generated policy for this run
    policy-comparison.md                          the policy as tables, against any hand-written one
    results/_NNN_/                                one harness run per episode (official_predictions.jsonl,
                                                  scores.jsonl, summary.json, run.log, exit-code.txt, ...)
    smoke-test/                                   offline smoke run made before launch
    summary.json                                  pooled scores (pipeline/compile_summary.py)
    gpt-4.1-rejudge/, gpt-4.1-strict-rejudge/     independent rejudges (pipeline/rejudge_*.{sh,py})
"""
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'data' / 'gatemem'
STRICT_JUDGE = REPO / 'data' / 'govmem-strict-judge'
DOMAINS = ('medical', 'office', 'education', 'household')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def episodes(domain, data=DATA):
    return read_jsonl(Path(data) / 'bench/data' / domain / 'episodes.jsonl')


def checkpoints(domain, data=DATA):
    return read_jsonl(Path(data) / 'bench/data' / domain / 'checkpoints.jsonl')


def episode_tag(episode_id):
    """'med_episode_rewrite_en_001_...' -> '_001_' (the --episode-contains selector)."""
    tags = re.findall(r'_(\d{3})_', episode_id)
    if len(tags) != 1:
        raise ValueError(f'episode ID without exactly one _NNN_ tag: {episode_id}')
    return f'_{tags[0]}_'


def episode_tags(domain, data=DATA):
    return [episode_tag(e['episode_id']) for e in sorted(episodes(domain, data), key=lambda e: e['episode_id'])]


def run_domain(run_dir):
    """The domain a run directory belongs to, from its frozen policy or any episode manifest."""
    run_dir = Path(run_dir)
    if (run_dir / 'role_policy.json').is_file():
        return json.loads((run_dir / 'role_policy.json').read_text())['domain']
    for manifest in sorted((run_dir / 'results').glob('*/manifest.json')):
        return json.loads(manifest.read_text()).get('domain', 'medical')
    raise SystemExit(f'cannot tell the domain of {run_dir}: no role_policy.json and no episode manifest')


def episode_dirs(run_dir):
    results = Path(run_dir) / 'results'
    return sorted(p for p in results.iterdir() if p.is_dir()) if results.is_dir() else []


def combined_predictions(run_dir, domain, data=DATA):
    """Every episode's official predictions as JSONL lines, in the dataset's checkpoint order.

    Returns (lines, per_episode_files). Refuses duplicates and any gap against the dataset.
    For the medical run this reproduces byte for byte the source of the GPT-4.1 rejudge
    (official_predictions sha256 07a4554f...).
    """
    order = [c['checkpoint_id'] for c in checkpoints(domain, data)]
    lines, files = {}, []
    for d in episode_dirs(run_dir):
        path = d / 'official_predictions.jsonl'
        if not path.is_file():
            raise SystemExit(f'{d.name}: no official_predictions.jsonl (episode not finished?)')
        files.append({'episode': d.name, 'manifest_sha256': sha256(d / 'manifest.json'),
                      'official_predictions_sha256': sha256(path)})
        for line in path.read_text().splitlines(keepends=True):
            if line.strip():
                cid = json.loads(line)['checkpoint_id']
                if cid in lines:
                    raise SystemExit(f'checkpoint predicted twice: {cid}')
                lines[cid] = line if line.endswith('\n') else line + '\n'
    missing = [c for c in order if c not in lines]
    extra = sorted(set(lines) - set(order))
    if missing or extra:
        raise SystemExit(f'predictions do not cover the {domain} dataset exactly: '
                         f'{len(missing)} missing, {len(extra)} unknown (first missing: {missing[:3]})')
    return [lines[c] for c in order], files


def bench_scorer(data=DATA):
    """The frozen GateMem scorer module (bench.eval.scorer) from the vendored benchmark."""
    if str(data) not in sys.path:
        sys.path.insert(0, str(data))
    from bench.eval import scorer
    return scorer


def headline(scores, data=DATA):
    """U/A/F/OR/MGS and their strict forms, pooled over checkpoints with the benchmark's aggregate()."""
    m = bench_scorer(data).aggregate(scores)
    count = lambda kind, key: sum(s.get(key) is True for s in scores if s['query_type'] == kind)
    return {
        'U': m['utility_accuracy'], 'A': m['privacy_leakage_rate'], 'F': m['deletion_leakage_rate'],
        'OR': m['over_refusal_rate'], 'MGS': m['compliance_utility_score'],
        'A_strict': m['privacy_e2e_leakage_rate'], 'F_strict': m['deletion_e2e_leakage_rate'],
        'MGS_strict': m['compliance_utility_e2e_score'], 'action_accuracy': m['action_accuracy'],
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
