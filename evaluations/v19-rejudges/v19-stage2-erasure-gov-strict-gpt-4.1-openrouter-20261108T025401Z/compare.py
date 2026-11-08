#!/usr/bin/env python3
"""Validate this strict rejudge and compare it with the strict V19 GPT-4.1 job. Writes validation_and_comparison.json."""
from datetime import datetime, timezone
import json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'frozen-govmem/src'))
from govmem.eval.judge import aggregate  # noqa: E402
V19_STRICT = Path('/home/thaddus/a-mem-variant/evaluations/v19-rejudges/v19-gov-strict-gpt-4.1-openrouter-20261107T023151Z')
load = lambda p: [json.loads(l) for l in Path(p).open() if l.strip()]
rows, base = load(HERE / 'verdicts.jsonl'), load(V19_STRICT / 'verdicts.jsonl')
summary = json.loads((HERE / 'summary.json').read_text())
preds = {p['checkpoint_id']: p for p in load(HERE / 'inputs/predictions.jsonl')}
short = lambda c: (lambda m: f'ep{m[1]} ck{m[2]}')(re.match(r'med_episode_rewrite_en_(\d+)_.*_ckpt_(\d+)$', c))


def lists(rs):
    pick = lambda kind, f: sorted(short(r['checkpoint_id']) for r in rs if r['query_type'] == kind and f(r))
    return {'privacy_answer_leaks': pick('privacy', lambda r: r['privacy_leak']),
            'deletion_answer_leaks': pick('safety', lambda r: r['deletion_leak']),
            'privacy_context_exposed': pick('privacy', lambda r: r['context_exposed']),
            'deletion_context_exposed': pick('safety', lambda r: r['context_exposed']),
            'privacy_strict_only': pick('privacy', lambda r: r['context_exposed'] and not r['privacy_leak']),
            'deletion_strict_only': pick('safety', lambda r: r['context_exposed'] and not r['deletion_leak'])}


def flips(key):
    b = {r['checkpoint_id']: r for r in base}
    out = {'now_pass': [], 'now_fail': []}
    for r in rows:
        if r['query_type'] != {'utility_correct': 'utility', 'privacy_leak': 'privacy', 'deletion_leak': 'safety'}[key]:
            continue
        good = (lambda v: v) if key == 'utility_correct' else (lambda v: not v)
        old, new = good(b[r['checkpoint_id']][key]), good(r[key])
        if old != new:
            out['now_pass' if new else 'now_fail'].append(short(r['checkpoint_id']))
    return {k: sorted(v) for k, v in out.items()} | {'n_now_pass': len(out['now_pass']), 'n_now_fail': len(out['now_fail'])}


m, mb = aggregate(rows), aggregate(base)
keys = ('U', 'A', 'F', 'OR', 'MGS', 'A_strict', 'F_strict', 'MGS_strict')
report = {
    'validated_utc': datetime.now(timezone.utc).isoformat(),
    'complete': summary['complete'],
    'checkpoint_counts': {k: sum(r['query_type'] == k for r in rows) for k in ('utility', 'privacy', 'safety')},
    'all_llm_judged_no_fallback': all(r['llm_judged'] and not r['judge_fallback'] for r in rows),
    'order_matches_predictions': [r['checkpoint_id'] for r in rows] == list(preds),
    'aggregate_reproduced': m == summary['metrics'],
    'usage': json.loads((HERE / 'usage.json').read_text()),
    'metrics': m,
    'metrics_sensitivity_answer_model_called_only': summary['sensitivity_answer_model_called_only'],
    'v19_strict_gpt41_metrics': mb,
    'this_run_minus_v19_strict_gpt41': {k: m[k] - mb[k] for k in keys},
    'checkpoints': lists(rows),
    'v19_strict_gpt41_checkpoints': lists(base),
    'checkpoint_flips_vs_v19_strict_gpt41': {k: flips(k) for k in ('utility_correct', 'privacy_leak', 'deletion_leak')},
    'counts': {k: len(v) for k, v in lists(rows).items()},
    'v19_strict_gpt41_counts': {k: len(v) for k, v in lists(base).items()},
}
(HERE / 'validation_and_comparison.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({k: report[k] for k in ('complete', 'all_llm_judged_no_fallback', 'order_matches_predictions', 'aggregate_reproduced', 'counts', 'v19_strict_gpt41_counts')}, indent=1))
print('this', {k: round(m[k], 4) for k in keys}); print('v19 ', {k: round(mb[k], 4) for k in keys})
print(json.dumps(report['checkpoints'], indent=0)); print(json.dumps(report['checkpoint_flips_vs_v19_strict_gpt41']))
