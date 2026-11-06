"""Offline forensic export. Live runs are opened read-only; no model calls."""
import collections
from copy import deepcopy
import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parent
RUNS = Path('/home/thaddus/a-mem-variant/runs')
DATA = Path('/home/thaddus/GateMem/bench/data/medical')
RUN_NAMES = {
    'v19': 'gatemem-medical-v19-openrouter-full-20261102T005006+0800',
    'v21': 'gatemem-medical-v21-gpu234-full-20261102T182855+0800',
}

def read_jsonl(path):
    # A live writer may leave a final partial line; do not invent its contents.
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if line != path.read_text().splitlines()[-1]:
                raise
    return out

def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(body, indent=2) + '\n')
    path.chmod(0o600)

def decode(row):
    result = dict(row)
    for k, v in result.items():
        if isinstance(v, str) and v[:1] in ('{', '['):
            try:
                result[k] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return result

def stats(vals):
    vals = sorted(vals)
    return {'count': len(vals), 'mean': statistics.mean(vals) if vals else None,
            'median': statistics.median(vals) if vals else None,
            'p90': vals[int(.9*(len(vals)-1))] if vals else None,
            'p95': vals[int(.95*(len(vals)-1))] if vals else None,
            'max': max(vals, default=None), 'over_30': sum(v > 30 for v in vals)}

def compact_case(case):
    """Avoid duplicating full binding-version history in every checkpoint file.

    Original unabridged records/attempts remain in the fixed SQLite backup and
    original model traces. Selected control receipts are also in readable cards.
    """
    case = deepcopy(case)
    case.pop('permission_record_history_through_checkpoint', None)
    for job in case.get('permission_jobs_through_checkpoint', []):
        validation = job.get('validation')
        if isinstance(validation, dict):
            for key in ['candidate_binding_decisions', 'pending_bindings']:
                validation.pop(key, None)
        proposal = job.get('proposal')
        if isinstance(proposal, dict):
            proposal['operations'] = [op for op in proposal.get('operations', []) if op.get('op') != 'bind_information']
    for attempt in case.get('permission_attempts_through_checkpoint', []):
        proposal = attempt.get('proposal')
        if isinstance(proposal, dict):
            proposal.pop('binding_reviews', None)
            proposal.pop('segments', None)
    case['unabridged_ledger'] = {
        'database': str(ROOT/'snapshot'/case['version']/'memory.privacy.sqlite'),
        'tables': ['permission_records', 'permission_jobs', 'permission_attempts'],
        'compartment': case['compartment'],
        'cutoff': case['checkpoint']['as_of_turn_id'],
        'note': 'Repeated full binding histories omitted from this export, retained in the immutable audit backup. Exact query packet and model responses remain in this case.'}
    return case

def main():
    episodes = {x['episode_id']: x for x in read_jsonl(DATA/'episodes.jsonl')}
    checkpoints = {x['checkpoint_id']: x for x in read_jsonl(DATA/'checkpoints.jsonl')}
    overview = {}
    for ver, name in RUN_NAMES.items():
        source = RUNS/name
        dest = ROOT/'snapshot'/ver
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (dest/'inventory.json').exists():
            overview[ver] = json.loads((dest/'inventory.json').read_text())
            continue
        at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        # Each backup is transactionally consistent on its own; the three files
        # are not claimed to be a single cross-database atomic checkpoint.
        for db in ['evaluation.sqlite', 'memory.sqlite', 'memory.privacy.sqlite']:
            ro = sqlite3.connect(f'file:{source/db}?mode=ro', uri=True)
            target = sqlite3.connect(dest/db)
            ro.backup(target)
            target.close()
            ro.close()
            (dest/db).chmod(0o600)
        for file in ['summary.json', 'manifest.json', 'effective_manifest.json', 'recovery.json']:
            if (source/file).exists():
                (dest/file).write_bytes((source/file).read_bytes())
                (dest/file).chmod(0o600)
        traces = read_jsonl(source/'model-trace.jsonl')
        # Preserve the captured prefix, avoiding reads of later runtime state.
        (dest/'model-trace.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in traces))
        (dest/'model-trace.jsonl').chmod(0o600)
        ev = sqlite3.connect(f'file:{dest/"evaluation.sqlite"}?mode=ro', uri=True)
        ev.row_factory = sqlite3.Row
        predictions, scores = {}, {}
        for row in ev.execute('SELECT * FROM official ORDER BY boundary,id'):
            predictions[row['id']] = json.loads(row['prediction'])
            if row['score']:
                scores[row['id']] = json.loads(row['score'])
        ev.close()
        pr = sqlite3.connect(f'file:{dest/"memory.privacy.sqlite"}?mode=ro', uri=True)
        pr.row_factory = sqlite3.Row
        me = sqlite3.connect(f'file:{dest/"memory.sqlite"}?mode=ro', uri=True)
        me.row_factory = sqlite3.Row
        resources = {r['id']: decode(r) for r in me.execute('SELECT * FROM resources')}
        jobs = {r['id']: decode(r) for r in pr.execute('SELECT rowid,* FROM permission_jobs ORDER BY rowid')}
        sources = {i: r for i, r in resources.items() if r['kind'] == 'observation' and isinstance(r['payload'], dict) and 'source' in r['payload']}
        candidates = [decode(r) for r in pr.execute('SELECT rowid,* FROM fact_candidates ORDER BY rowid')]
        records = [decode(r) for r in pr.execute('SELECT rowid,* FROM permission_records ORDER BY rowid')]
        attempts = [decode(r) for r in pr.execute('SELECT rowid,* FROM permission_attempts ORDER BY rowid')]
        def turn_of_source(sid):
            return sources.get(sid, {}).get('payload', {}).get('source', {}).get('event_id')
        for job in jobs.values():
            job['event_id'] = turn_of_source(job['source_id'])
            job['input_event_max'] = max((turn_of_source(s) or '' for s in job['inputs']), default='')
        query_trace = collections.defaultdict(list)
        for i, tr in enumerate(traces):
            pay = tr.get('payload', {})
            if isinstance(pay, dict) and 'query' in pay and 'requester_id' in pay:
                query_trace[(pay['query'], pay['requester_id'], pay.get('as_of_turn_id'))].append({'trace_line':i+1, **tr})
        cases = []
        for cid, pred in predictions.items():
            cp = checkpoints[cid]
            if cp['query_type'] != 'utility' and not any(scores.get(cid, {}).get(k) for k in ['privacy_leak', 'privacy_context_leak']):
                continue
            ep, cutoff = cp['episode_id'], cp['as_of_turn_id']
            turns = [t for t in episodes[ep]['turns'] if t['turn_id'] <= cutoff]
            # Use the compartment from observations for this episode, identified
            # by the dataset source namespace and participant/turn/text match.
            first = episodes[ep]['turns'][0]
            matching = [r for r in sources.values() if r['payload']['source'].get('event_id') == first['turn_id'] and r['payload']['source'].get('participant') == first['speaker']['principal_id'] and r['payload'].get('text') == first['text']]
            compartment = matching[0]['compartment'] if matching else None
            if not compartment:
                # Erased first observations can be scrubbed: deterministic adapter key.
                compartment = 'encounter:' + hashlib.sha256(ep.encode()).hexdigest()[:24]
            eligible_jobs = {i:j for i,j in jobs.items() if j['compartment']==compartment and j['input_event_max'] <= cutoff}
            latest = {}
            history = []
            for rec in records:
                if rec['job_id'] in eligible_jobs:
                    history.append(rec)
                    latest[(rec['kind'],rec['id'])] = rec
            asker = cp['asker']['principal_id']
            assignments = [r for (kind,_),r in latest.items() if kind=='assignment' and r['body'].get('participant')==asker]
            grants = {r['id'] for r in assignments}
            subject_ids = {r['body'].get('subject') for r in assignments}
            relevant_candidates = [r for r in candidates if r['compartment']==compartment and (turn_of_source(r['source_id']) or 'z') <= cutoff]
            relevant_reviews = []
            # Per-job decisions are publication receipts, unlike fact_processing,
            # whose latest value can include later binding/revocation changes.
            for j in eligible_jobs.values():
                for d in (j.get('validation') or {}).get('candidate_binding_decisions', []):
                    if d.get('assignment_id') in grants:
                        relevant_reviews.append({'job_id':j['id'],'event_id':j['event_id'],**d})
            relevant_attempts = [a for a in attempts if a['job_id'] in eligible_jobs]
            raw = query_trace.get((cp['query_text'],asker,cutoff), [])
            case = {'version':ver,'checkpoint':cp,'score':scores.get(cid), 'prediction':pred,
                    'compartment':compartment,'original_turns_through_checkpoint':turns,
                    'stored_candidates_through_checkpoint':relevant_candidates,
                    'asker_assignment_versions_reconstructed':assignments,
                    'asker_review_receipts_through_checkpoint':relevant_reviews,
                    'permission_record_history_through_checkpoint':history,
                    'permission_jobs_through_checkpoint':list(eligible_jobs.values()),
                    'permission_attempts_through_checkpoint':relevant_attempts,
                    'answer_model_calls':raw,
                    'forensic_limits':[
                        'Exact query packet and output come from historical prediction/trace, never reconstructed from current retrieval.',
                        'Original benchmark turns are expected-evidence provenance, not proof that extraction retained those facts.',
                        'Assignment reconstruction uses job input-prefix cutoff. V19 has no host historical recovery; V21 historical recovery can publish later and requires receipt ordering or turn-54 backup for definitive transition timing.',
                        'Candidate bodies and old receipts can have been scrubbed by later erasure. Missing quote is not proof the fact never existed.',
                        'Snapshots are individually consistent SQLite backups, not a cross-database atomic snapshot.'
                    ]}
            short = re.search(r'en_(\d+)_',cid).group(1)+'-'+cid.rsplit('_',1)[-1]
            write(ROOT/'cases'/ver/(short+'.json'),compact_case(case))
            cases.append({'case':short,'checkpoint_id':cid,'query_type':cp['query_type'],'turn':cutoff,'asker':asker,'query':cp['query_text'],'expected':cp['judge_spec'],
                'action':pred['output']['action'],'utility_correct':scores.get(cid,{}).get('utility_correct'),
                'over_refusal':scores.get(cid,{}).get('over_refusal'),
                'packet_count':len(pred['output']['retrieved_memory']),
                'processing':pred.get('input_manifest',{}).get('processing',{}),
                'assignment_statuses':collections.Counter(r['body'].get('status') for r in assignments),
                'owner_status':[(r['id'],r['body'].get('status')) for r in assignments if r['body'].get('ownership')],
                'answer':pred['output']['answer']})
        durations = [r[0] for r in me.execute("SELECT seconds FROM pipeline_timings WHERE stage='source_total'")]
        timing = {'all_sources': stats(durations), 'model_stages': {}}
        for stage in sorted({t['stage'] for t in traces}):
            rows = [t for t in traces if t['stage']==stage]
            timing['model_stages'][stage] = {'seconds':stats([t['seconds'] for t in rows if isinstance(t.get('seconds'),(int,float))]),
                'prompt_tokens':stats([(t.get('usage') or {}).get('prompt_tokens',0) for t in rows]),
                'completion_tokens':stats([(t.get('usage') or {}).get('completion_tokens',0) for t in rows]),
                'providers':dict(collections.Counter(t.get('provider') or t.get('response_model') for t in rows)),
                'errors':dict(collections.Counter(t.get('error_type') for t in rows if t.get('error_type')))}
        # Matched early episode prefix from actual source-job timings.
        first_comp = next(iter(jobs.values()))['compartment']
        first_jobs = [j for j in jobs.values() if j['compartment']==first_comp and j['id'].startswith('permission-job:') and j['event_id'] and j['event_id']<='t134']
        timing['first_episode_permission_job_wall_time'] = stats([(j.get('validation') or {}).get('stage_timings',{}).get('update_seconds',(j.get('validation') or {}).get('elapsed_s',0)) for j in first_jobs])
        result = {'captured_at':at,'run':str(source),'summary':json.loads((dest/'summary.json').read_text()),'cases':cases,'timing':timing,'model_calls':len(traces),'no_model_calls_made_by_audit':True}
        write(dest/'inventory.json',result)
        overview[ver] = result
        pr.close();me.close()
    write(ROOT/'inventory.json',overview)
    print(json.dumps({v:{'captured_at':x['captured_at'],'cases':len(x['cases']),'timing':x['timing']['all_sources']} for v,x in overview.items()},indent=2))

if __name__ == '__main__':
    main()
