#!/usr/bin/env python3
"""Progress of a run and its pooled scores so far. Read-only; safe while episodes run.

    python pipeline/status.py runs/<domain>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

ANSWERS = ('answer', 'answer_redacted')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit(__doc__)
    run = Path(argv[0])
    domain = common.run_domain(run)
    checks = {c['checkpoint_id']: c for c in common.checkpoints(domain)}
    tags = common.episode_tags(domain)
    seen = {d.name: d for d in common.episode_dirs(run)}
    rows = []
    print(f'{domain}: {len(seen)}/{len(tags)} episodes started')
    print(f"{'ep':6} {'turns':>9} {'ckpts':>7} {'util':>6} {'exit':>5}")
    for tag in tags:
        d = seen.get(tag)
        if d is None:
            print(f'{tag:6} queued')
            continue
        s = d / 'summary.json'
        if not s.exists():
            print(f'{tag:6} starting')
            continue
        j = json.loads(s.read_text())
        c, m = j['coverage'], j['metrics']
        u = m.get('utility_accuracy')
        x = d / 'exit-code.txt'
        print(f"{tag:6} {c['ingested_source_turns']:>4}/{c['expected_source_turns']:<4} "
              f"{c['official_predictions']:>3}/{c['expected_checkpoints']:<3} "
              f"{'-' if u is None else format(u, '.2f'):>6} {x.read_text().strip() if x.exists() else 'run':>5}")
        if (d / 'scores.jsonl').exists():
            rows += common.read_jsonl(d / 'scores.jsonl')
    kind = lambda name: [r for r in rows if r['query_type'] == name]
    u, p, f = kind('utility'), kind('privacy'), kind('safety')
    print(f'\ncheckpoints scored: {len(rows)}/{len(checks)}')
    print(f"utility:        {sum(r['utility_correct'] is True for r in u)}/{len(u)}")
    print(f"over-refusal:   {sum(r['pred_action'] not in ANSWERS for r in u)}/{len(u)}")
    print(f"privacy leaks:  {sum(r.get('privacy_leak') is True for r in p)}/{len(p)}")
    print(f"deletion leaks: {sum(r.get('deletion_leak') is True for r in f)}/{len(f)}")
    print(f"answered a refuse-expected access question: "
          f"{sum(r['pred_action'] in ANSWERS and checks[r['checkpoint_id']]['expected_action'] not in ANSWERS for r in p)}/{len(p)}")
    print(f"action correct: {sum(bool(r['action_correct']) for r in rows)}/{len(rows)}")
    if rows:
        h = common.headline(rows)
        print(f"pooled so far:  U={h['U']:.4f} A={h['A']:.4f} F={h['F']:.4f} MGS={h['MGS']:.4f}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
