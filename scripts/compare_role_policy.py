#!/usr/bin/env python3
"""Print an inferred role policy next to the hand-written host table, plus a stability report.

usage: compare_role_policy.py <inferred dir> [<fixed-kinds diagnostic dir>]
"""
import json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evaluation/gatemem_medical'))
from hosts import OFFICE as HAND


def load(d):
    d = Path(d)
    primary = json.loads((d / 'policy.json').read_text())
    samples = [json.loads(l) for l in (d / 'samples.jsonl').open() if l.strip()] if (d / 'samples.jsonl').exists() else []
    return primary, samples


def grid(kinds, rows, titles):
    head = '| title | ' + ' | '.join(kinds) + ' |'
    out = [head, '|' + '---|' * (len(kinds) + 1)]
    for t in titles:
        out.append(f'| {t} | ' + ' | '.join('✓' if k in rows.get(t, ()) else '' for k in kinds) + ' |')
    return '\n'.join(out)


def stability(primary, samples, titles):
    """Per title: how many samples give exactly the primary row (only meaningful when kinds match)."""
    lines = []
    for t in titles:
        same = sum(set(s['role_kinds'].get(t, [])) == set(primary['role_kinds'][t]) for s in samples)
        lines.append(f'{t}: {same}/{len(samples)}')
    auth = sum(set(s['authorities']) == set(primary['authorities']) for s in samples)
    kinds_same = sum(set(s['information_kinds']) == set(primary['information_kinds']) for s in samples)
    return lines, auth, kinds_same


inferred, samples = load(sys.argv[1])
titles = sorted(inferred['role_kinds'])
print('## Inferred kinds of information\n')
for k, v in inferred['information_kinds'].items():
    print(f'- **{k}**: {v}')
print('\n## Inferred table\n')
print(grid(list(inferred['information_kinds']), inferred['role_kinds'], titles))
print('\nAuthorities:', ', '.join(inferred['authorities']), '\n  reason:', inferred['authorities_reason'])
print('\nReasons:')
for t in titles:
    print(f'- {t}: {inferred["role_reasons"][t]}')
lines, auth, ks = stability(inferred, samples, titles)
print(f'\nStability over {len(samples)} samples: same kind names {ks}/{len(samples)}, same authorities {auth}/{len(samples)}')
print('Kind-name frequency across samples:', dict(Counter(k for s in samples for k in s['information_kinds']).most_common()))

print('\n## Hand-written table (current run)\n')
print(grid(list(HAND.information_types), HAND.role_information_types, titles))
print('\nAuthorities:', ', '.join(HAND.subject_authority_roles))

if len(sys.argv) > 2:
    diag, dsamples = load(sys.argv[2])
    print('\n## Diagnostic: model fills the CURRENT 10 kinds\n')
    print(grid(list(HAND.information_types), diag['role_kinds'], titles))
    print('\nAuthorities:', ', '.join(diag['authorities']))
    print('\nCell differences vs hand table (+ model allows, hand does not; - hand allows, model does not):')
    total = 0
    for t in titles:
        h, m = set(HAND.role_information_types.get(t, ())), set(diag['role_kinds'][t])
        if h != m:
            total += len(h ^ m)
            print(f'- {t}: ' + ' '.join([f'+{k}' for k in sorted(m - h)] + [f'-{k}' for k in sorted(h - m)]))
    print(f'{total} of {len(titles) * len(HAND.information_types)} cells differ')
    lines, auth, _ = stability(diag, dsamples, titles)
    print(f'Row stability over {len(dsamples)} samples:', '; '.join(lines), f'| authorities {auth}/{len(dsamples)}')
