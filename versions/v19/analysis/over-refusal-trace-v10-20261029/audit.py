wai """Read-only retrospective trace; never loads a model or changes run artifacts."""
import collections
import hashlib
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / 'runs/gatemem-medical-raw-stream-v10-full-20261029T105316+0800'
DATA = Path('/home/thaddus/GateMem/bench/data/medical')
OUT = Path(__file__).resolve().parent


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    checkpoints = read_jsonl(DATA / 'checkpoints.jsonl')
    predictions = {p['checkpoint_id']: p for p in read_jsonl(RUN / 'official_predictions.jsonl')}
    scores = {s['checkpoint_id']: s for s in read_jsonl(RUN / 'scores.jsonl')}
    selected = [c for c in checkpoints if c['episode_id'].startswith('med_episode_rewrite_en_001_')
                and c['checkpoint_id'].endswith(('_05', '_07'))]
    episode = next(e for e in read_jsonl(DATA / 'episodes.jsonl') if e['episode_id'] == selected[0]['episode_id'])
    turns = {t['turn_id']: t for t in episode['turns']}
    position = {tid: i for i, tid in enumerate(turns)}
    db = sqlite3.connect((RUN / 'memory.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('ATTACH DATABASE ? AS privacy', ((RUN / 'memory.privacy.sqlite').resolve().as_uri() + '?mode=ro',))
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    resources = {r['id']: dict(r) for r in db.execute('SELECT * FROM resources')}
    sources = {rid: json.loads(r['payload']) for rid, r in resources.items() if r['kind'] == 'observation'}
    source_turn = {rid: b['source']['event_id'] for rid, b in sources.items()
                   if b['source']['event_id'] in turns
                   and b.get('source', {}).get('context', {}).get('episode_id', episode['episode_id']) == episode['episode_id']}
    # Restrict to the encounter selected by the first checkpoint's saved source.
    from sys import path
    path.insert(0, str(ROOT))
    from agentic_memory.privacy.system import digest
    compartment = 'encounter:' + digest(episode['episode_id'])[:24]
    source_turn = {sid: tid for sid, tid in source_turn.items() if resources[sid]['compartment'] == compartment}
    jobs = {r['id']: dict(r) for r in db.execute('SELECT * FROM permission_jobs WHERE compartment=?', (compartment,))}
    jobs_by_turn = {source_turn[j['source_id']]: j for j in jobs.values() if j['source_id'] in source_turn}
    versions = [dict(r) for r in db.execute('SELECT * FROM permission_records WHERE compartment=? ORDER BY rowid', (compartment,))]
    attempts = collections.defaultdict(list)
    for row in db.execute('SELECT * FROM permission_attempts'):
        if row['job_id'] in jobs:
            attempts[row['job_id']].append(dict(row))
    coverage = collections.defaultdict(dict)
    for row in db.execute('SELECT * FROM permission_coverage'):
        if row['job_id'] in jobs:
            coverage[row['job_id']][row['principal']] = json.loads(row['body'])

    def at_or_before(job_id, cutoff):
        return position[source_turn[jobs[job_id]['source_id']]] <= position[cutoff]

    def state_at(cutoff):
        state = collections.defaultdict(dict)
        for record in versions:
            if at_or_before(record['job_id'], cutoff):
                state[record['kind']][record['id']] = json.loads(record['body'])
        return state

    def turn_audit(tid):
        job = jobs_by_turn[tid]
        attempt = max(attempts[job['id']], key=lambda a: a['attempt'])
        manifest = json.loads(attempt['input_manifest'])
        proposal = json.loads(attempt['proposal'] or '{}')
        validation = json.loads(attempt['validation'] or '{}')
        prior = state_at(turns_list[position[tid] - 1]) if position[tid] else {}
        return {
            'turn_id': tid,
            'source': {'speaker': turns[tid]['speaker']['principal_id'], 'text': turns[tid]['text']},
            'accepted': validation.get('accepted'),
            'attempt': attempt['attempt'],
            'loaded_source_turns': sorted(source_turn[s] for s in manifest['source_digests'] if s in source_turn),
            'operations': [{'op': o['op'], 'participant': o.get('participant'), 'id': o.get('id'),
                            'assignment_id': o.get('assignment_id'),
                            'recipient': prior.get('assignment', {}).get(o.get('assignment_id'), {}).get('participant'),
                            'fact_source_turn': source_turn.get(o.get('source_id')),
                            'necessity': o.get('necessity')} for o in proposal.get('operations', [])],
            'coverage': [{**body, 'evidence': [source_turn.get(s, s) for s in body['evidence']]}
                         for body in coverage[job['id']].values()],
        }

    turns_list = list(turns)
    cases = []
    for cp in selected:
        cutoff = cp['as_of_turn_id']
        principal = cp['asker']['principal_id']
        state = state_at(cutoff)
        principal_records = {kind: [r for r in state[kind].values() if r.get('participant') == principal]
                             for kind in ('activity', 'assignment')}
        matching_attempts = []
        for jid, rows in attempts.items():
            if not at_or_before(jid, cutoff):
                continue
            for row in rows:
                proposal = json.loads(row['proposal'] or '{}')
                if any(o.get('participant') == principal for o in proposal.get('operations', [])):
                    matching_attempts.append({'turn_id': source_turn[jobs[jid]['source_id']], 'attempt': row['attempt']})
        fact_turns = ['t074', 't076', 't110', 't111', 't112'] if cutoff == 't112' else ['t069', 't071', 't072', 't091', 't144']
        fact_recipients = []
        for tid in fact_turns:
            recipients = []
            for aid, assignment in state['assignment'].items():
                bindings = [b for b in assignment.get('bindings', []) if source_turn.get(b['source_id']) == tid]
                if bindings:
                    recipients.append({'principal': assignment['participant'], 'assignment_id': aid,
                                       'status': assignment['status'], 'binding_count': len(bindings)})
            fact_recipients.append({'turn_id': tid, 'recipients': recipients})
        trace_turns = (['t078', 't079', 't087', 't088', 't097', 't099', 't100', 't101', 't110', 't111', 't112']
                       if cutoff == 't112' else ['t069', 't070', 't071', 't091', 't092', 't116', 't117', 't120', 't144'])
        case = {'checkpoint': cp, 'prediction': predictions[cp['checkpoint_id']],
                'score': {k: scores[cp['checkpoint_id']][k] for k in ('utility_correct', 'over_refusal', 'pred_action')},
                'prefix_source_count': position[cutoff] + 1,
                'requester_activity_count': len(principal_records['activity']),
                'requester_assignment_count': len(principal_records['assignment']),
                'attempts_proposing_requester_involvement_or_assignment': matching_attempts,
                'facts_bound_to': fact_recipients,
                'turn_trace': [turn_audit(tid) for tid in trace_turns]}
        # These check the causal diagnosis against persisted history, including rejected attempts.
        assert not principal_records['activity'] and not principal_records['assignment']
        assert not matching_attempts
        assert case['prediction']['output']['action'] == 'refuse'
        assert not case['prediction']['input_manifest']['memory_ids']
        assert case['score']['over_refusal'] is True
        assert all(principal not in [r['principal'] for r in fact['recipients']] for fact in fact_recipients)
        cases.append(case)

    nurse = {t['turn_id']: t for t in cases[0]['turn_trace']}
    pharmacist = {t['turn_id']: t for t in cases[1]['turn_trace']}
    assert {'t097', 't099'} <= set(nurse['t099']['loaded_source_turns'])
    assert {'t100', 't101'} <= set(nurse['t101']['loaded_source_turns'])
    assert {'t116', 't117'} <= set(pharmacist['t117']['loaded_source_turns'])
    assert pharmacist['t117']['accepted'] is True
    assert not any(c['principal'] == 'nurse_casey_liu' for c in nurse['t112']['coverage'])
    assert next(c for c in pharmacist['t116']['coverage'] if c['principal'] == 'pharmacist_owens')['decision'] == 'unresolved'
    assert not any(c['principal'] == 'pharmacist_owens' for c in pharmacist['t120']['coverage'])
    result = {
        'method': 'Retrospective read-only SQL audit. Ledger versions filtered by each checkpoint source prefix. No model calls, score changes, or runtime edits.',
        'run': str(RUN),
        'dataset_hashes': {name: hashlib.sha256((DATA / name).read_bytes()).hexdigest()
                           for name in ('episodes.jsonl', 'checkpoints.jsonl')},
        'checks': 'All causal assertions passed.',
        'cases': cases,
    }
    db.rollback()
    db.close()
    (OUT / 'trace.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'cases': len(cases), 'checks': result['checks'], 'output': str(OUT / 'trace.json')}))


if __name__ == '__main__':
    main()
