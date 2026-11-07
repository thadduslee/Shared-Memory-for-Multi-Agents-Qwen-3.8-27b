"""Recompute attribution for a finished run's saved model decisions; no model calls.

The saved decisions came from a prompt without names, roles or mentioned_people, so
this estimates the citation rules only. Stores are opened read-only.

Usage: python analysis/v19-attribution-citation/replay.py RUN_DIR
"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
import re
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agentic_memory.privacy.attribution_v19 import compile_attribution
from evaluation.gatemem_medical.inference_adapter import WORKER_ROLES
from evaluation.gatemem_medical.input_boundary import identity_directory


def connect(path):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def turn_number(turn_id):
    return int(re.sub(r'\D', '', turn_id) or 0)


class Updater:
    """Only what compile_attribution reads from a live updater."""
    def __init__(self, directory):
        people = {pid: {} for pid in directory}
        self.bootstrap = SimpleNamespace(directory=directory, worker_roles=WORKER_ROLES, care_scopes={},
            resources=people, participants=people)

    def _source_bootstrap(self, sources):
        return self.bootstrap


def main(run_dir):
    run = Path(run_dir)
    data = run / 'frozen-dataset/bench/data/medical'
    episodes = {e['episode_id']: e for e in map(json.loads, (data / 'episodes.jsonl').open())}
    checkpoints = {c['checkpoint_id']: c for c in map(json.loads, (data / 'checkpoints.jsonl').open())}
    privacy, memory = connect(run / 'results/memory.privacy.sqlite'), connect(run / 'results/memory.sqlite')
    observed = defaultdict(dict)
    for row in memory.execute("SELECT id,compartment,payload FROM resources WHERE kind='observation' ORDER BY rowid"):
        observed[row['compartment']][row['id']] = json.loads(row['payload'])
    episode_of = {}
    for compartment, sources in observed.items():
        first = list(sources.values())[:5]
        for eid, episode in episodes.items():
            texts = {t['turn_id']: t['text'] for t in episode['turns']}
            if all(texts.get(s['source']['event_id']) == s['text'] for s in first):
                episode_of[compartment] = eid
    new, reasons, facts = Counter(), Counter(), defaultdict(list)
    for row in privacy.execute("SELECT * FROM fact_candidates WHERE state='active'"):
        body = json.loads(row['body']);old = body.get('attribution')
        compartment = row['compartment']
        if not old or compartment not in episode_of or row['source_id'] not in observed[compartment]:
            continue
        sources = observed[compartment];order = list(sources)
        prefix = {sid: sources[sid] for sid in order[:order.index(row['source_id']) + 1]}
        cited = [sid for sid in body.get('subject_evidence', {}) if sid != row['source_id'] and sid in prefix]
        decision = {'information_subjects': old.get('model_subjects') or [],
            'attribution_complete': bool(old.get('model_subjects')), 'action_actors': [], 'mentioned_people': [],
            'subject_source': cited[0] if cited else None,
            'subject_quote': prefix[cited[0]]['text'] if cited else None, 'reason': old.get('reason', '')}
        directory = identity_directory(episodes[episode_of[compartment]])
        after = compile_attribution(Updater(directory), {**body}, prefix[row['source_id']], prefix, decision)
        state = after['attribution']['state']
        new[old['state'], state] += 1
        if state == 'UNRESOLVED':
            reasons.update(' '.join(r.split()[:2]) for r in after['attribution']['unresolved'])
        facts[compartment].append({'old': old['state'], 'new': state, 'body': after, 'turn': body['source_ref']['event_id']})
    print('facts by (old state, new state):')
    for key, count in sorted(new.items()):
        print(f'  {key[0]:>10} -> {key[1]:<10} {count}')
    print('remaining unresolved reasons:', reasons.most_common())
    compartments = {v: k for k, v in episode_of.items()}
    stages = Counter();by_asker = Counter()
    for line in (run / 'results/scores.jsonl').open():
        score = json.loads(line)
        if score['query_type'] != 'utility':
            continue
        checkpoint = checkpoints[score['checkpoint_id']]
        role = checkpoint['asker']['role']
        asker = 'patient' if role == 'patient' else 'family' if role == 'family_member' else 'staff'
        limit = turn_number(checkpoint['as_of_turn_id'])
        visible = [f for f in facts[compartments[checkpoint['episode_id']]] if turn_number(f['turn']) <= limit]
        for pattern in checkpoint['judge_spec'].get('include', []):
            hits = [f for f in visible if re.search(pattern, f['body']['quote'], re.I)]
            stage = ('not extracted' if not hits else 'was already resolved' if any(f['old'] == 'RESOLVED' for f in hits)
                else 'newly resolved' if any(f['new'] == 'RESOLVED' for f in hits) else 'still unresolved')
            stages[stage] += 1;by_asker[asker, stage] += 1
    print('expected answer items:', stages.most_common())
    print('by asker:', sorted(by_asker.items()))
    primary_mix = Counter();decoys = []
    for compartment, rows in facts.items():
        patients = {p for p, v in identity_directory(episodes[episode_of[compartment]]).items() if v['role'] == 'patient'}
        for fact in rows:
            if fact['old'] == 'RESOLVED' or fact['new'] != 'RESOLVED':
                continue
            subjects = set(fact['body']['information_subjects'])
            kind = 'two patients' if len(subjects & patients) > 1 else 'one patient' if subjects & patients else 'no patient'
            primary_mix[kind] += 1
            names = [identity_directory(episodes[episode_of[compartment]])[p]['name'] for p in patients - subjects]
            if any(name.split()[0] in fact['body']['quote'] for name in names):
                decoys.append(fact)
    print('newly resolved subject mix:', primary_mix.most_common())
    print(f'newly resolved facts naming the other patient: {len(decoys)} (read these by hand)')
    for fact in decoys[:20]:
        print(f"  {fact['turn']} {fact['body']['author']} -> {fact['body']['information_subjects']}: {fact['body']['quote'][:110]}")
    rng = random.Random(19)
    pool = [f for rows in facts.values() for f in rows if f['old'] != 'RESOLVED' and f['new'] == 'RESOLVED']
    print('random sample of newly resolved facts:')
    for fact in rng.sample(pool, min(25, len(pool))):
        print(f"  {fact['turn']} {fact['body']['author']} -> {fact['body']['information_subjects']}: {fact['body']['quote'][:110]}")


if __name__ == '__main__':
    main(sys.argv[1])
