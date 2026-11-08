#!/usr/bin/env python3
"""Validate this rejudge and compare U/A/F/OR/MGS with the source run's original judge and with the
original V19 GPT-4.1 rejudge. Writes validation_and_comparison.json."""
from datetime import datetime, timezone
import hashlib, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'frozen-dataset'))
from bench.eval.scorer import aggregate  # noqa: E402
V19_GPT41 = Path('/home/thaddus/a-mem-variant/evaluations/v19-rejudges/v19-original-gpt-4.1-openrouter-20261107T022528Z')
load = lambda p: [json.loads(l) for l in Path(p).open() if l.strip()]
new, orig, base = load(HERE / 'scores.jsonl'), load(HERE / 'inputs/scores.jsonl'), load(V19_GPT41 / 'scores.jsonl')
preds = load(HERE / 'inputs/official_predictions.jsonl')
manifest = json.loads((HERE / 'manifest.json').read_text())


def uafm(rows):
    m = aggregate(rows)
    n = lambda kind, key: sum(r.get(key) is True for r in rows if r['query_type'] == kind)
    return {'U': m['utility_accuracy'], 'A': m['privacy_leakage_rate'], 'F': m['deletion_leakage_rate'],
            'OR': m['over_refusal_rate'], 'MGS': m['compliance_utility_score'],
            'A_context': m['privacy_context_leakage_rate'], 'F_context': m['deletion_context_leakage_rate'],
            'A_e2e': m['privacy_e2e_leakage_rate'], 'F_e2e': m['deletion_e2e_leakage_rate'],
            'MGS_e2e': m['compliance_utility_e2e_score'], 'action_accuracy': m['action_accuracy'],
            'counts': {'utility_correct': f"{n('utility', 'utility_correct')}/{m['n_utility_scored']}",
                       'privacy_leaks': f"{n('privacy', 'privacy_leak')}/{m['n_privacy_scored']}",
                       'deletion_leaks': f"{n('safety', 'deletion_leak')}/{m['n_safety_scored']}",
                       'privacy_context_leaks': f"{n('privacy', 'privacy_context_leak')}/{m['n_privacy_context_scored']}",
                       'deletion_context_leaks': f"{n('safety', 'deletion_context_leak')}/{m['n_safety_context_scored']}",
                       'over_refusals': f"{n('utility', 'over_refusal')}/{m['n_utility']}",
                       'action_correct': f"{sum(r.get('action_correct') is True for r in rows)}/{len(rows)}"}}


def short(cid):
    m = re.match(r'med_episode_rewrite_en_(\d+)_.*_ckpt_(\d+)$', cid)
    return f'ep{m[1]} ck{m[2]}'


def flips(a_rows, b_rows, key, good):
    a, b = {r['checkpoint_id']: r for r in a_rows}, {r['checkpoint_id']: r for r in b_rows}
    out = {'now_pass': [], 'now_fail': []}
    for cid in a:
        if a[cid].get(key) is None and b[cid].get(key) is None:
            continue
        ga, gb = good(a[cid].get(key)), good(b[cid].get(key))
        if ga != gb:
            out['now_pass' if gb else 'now_fail'].append(short(cid))
    return {k: sorted(v) for k, v in out.items()} | {'n_now_pass': len(out['now_pass']), 'n_now_fail': len(out['now_fail'])}


ok_true, ok_not_leak = (lambda v: v is True), (lambda v: v is not True)
by_id = {r['checkpoint_id']: r for r in orig}
hashes_ok = all(hashlib.sha256((HERE / k).read_bytes()).hexdigest() == v for k, v in manifest['input_sha256'].items()) and \
    all(hashlib.sha256(Path(k).read_bytes()).hexdigest() == v for k, v in manifest['original_files_sha256'].items())
summary = json.loads((HERE / 'summary.json').read_text())
r_new, r_orig, r_base = uafm(new), uafm(orig), uafm(base)
keys = ('U', 'A', 'F', 'OR', 'MGS', 'A_context', 'F_context', 'A_e2e', 'F_e2e', 'MGS_e2e', 'action_accuracy')
report = {
    'validated_utc': datetime.now(timezone.utc).isoformat(),
    'complete': summary['complete'] and len(new) == 579,
    'checkpoint_counts': {k: sum(r['query_type'] == k for r in new) for k in ('utility', 'privacy', 'safety')},
    'all_verdicts_present': all(isinstance(r.get('judge'), dict) for r in new),
    'all_original_auxiliary_scores_unchanged': all(r['auxiliary'] == by_id[r['checkpoint_id']]['auxiliary'] for r in new),
    'all_original_file_and_frozen_input_hashes_match': hashes_ok,
    'original_prediction_order_preserved': [r['checkpoint_id'] for r in new] == [p['checkpoint_id'] for p in preds],
    'aggregate_reproduced': aggregate(new) == summary['metrics'],
    'usage': json.loads((HERE / 'usage.json').read_text()),
    'metrics_gpt41_this_run': r_new,
    'metrics_original_judge_this_run': r_orig,
    'metrics_gpt41_original_v19': r_base,
    'gpt41_this_run_minus_original_judge_this_run': {k: r_new[k] - r_orig[k] for k in keys},
    'gpt41_this_run_minus_gpt41_original_v19': {k: r_new[k] - r_base[k] for k in keys},
    'checkpoint_flips_vs_original_judge_this_run': {
        'utility_correct': flips(orig, new, 'utility_correct', ok_true),
        'privacy_leak': flips(orig, new, 'privacy_leak', ok_not_leak),
        'deletion_leak': flips(orig, new, 'deletion_leak', ok_not_leak)},
    'checkpoint_flips_vs_gpt41_original_v19': {
        'utility_correct': flips(base, new, 'utility_correct', ok_true),
        'privacy_leak': flips(base, new, 'privacy_leak', ok_not_leak),
        'deletion_leak': flips(base, new, 'deletion_leak', ok_not_leak)},
    'leaks_gpt41_this_run': {
        'privacy': sorted(short(r['checkpoint_id']) for r in new if r.get('privacy_leak') is True),
        'deletion': sorted(short(r['checkpoint_id']) for r in new if r.get('deletion_leak') is True),
        'privacy_context': sorted(short(r['checkpoint_id']) for r in new if r.get('privacy_context_leak') is True),
        'deletion_context': sorted(short(r['checkpoint_id']) for r in new if r.get('deletion_context_leak') is True)},
}
(HERE / 'validation_and_comparison.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: v for k, v in report.items() if not k.startswith(('metrics_', 'usage'))}, indent=1))
for name in ('metrics_gpt41_this_run', 'metrics_original_judge_this_run', 'metrics_gpt41_original_v19'):
    m = report[name]; print(name, {k: round(m[k], 4) for k in keys}, m['counts'])
