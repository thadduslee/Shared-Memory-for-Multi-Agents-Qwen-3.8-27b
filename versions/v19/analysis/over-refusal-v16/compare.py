"""Compare the identical chronological prefix, including source-turn latency."""
import argparse
import json
from pathlib import Path
import sqlite3
import statistics

from evaluation.gatemem_medical.scoring import GateMemScorer


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def measured(run, predictions, scores, timing_exclusions=()):
    ids={p['checkpoint_id'] for p in predictions}
    selected=[s for s in scores if s['checkpoint_id'] in ids]
    assert len(selected)==len(ids)
    metrics=GateMemScorer('/home/thaddus/GateMem').aggregate(selected)
    memory=sqlite3.connect(f'file:{run}/memory.sqlite?mode=ro',uri=True)
    privacy=sqlite3.connect(f'file:{run}/memory.privacy.sqlite?mode=ro',uri=True)
    # Checkpoint source positions, NOT the live run's later global summary.
    cutoffs={}
    for p in predictions:
        cutoffs[p['episode_id']]=max(cutoffs.get(p['episode_id'],0),int(p['as_of_turn_id'][1:]))
    from agentic_memory.privacy.system import digest
    compartments={'encounter:'+digest(eid)[:24]:end for eid,end in cutoffs.items()}
    episode_by_compartment={'encounter:'+digest(eid)[:24]:eid for eid in cutoffs}
    source_ids=set();excluded_ids=set()
    for sid,comp,payload in memory.execute("SELECT id,compartment,payload FROM resources WHERE kind='observation'"):
        event=json.loads(payload)['source']['event_id']
        if comp in compartments and int(event[1:])<=compartments[comp]:
            source_ids.add(sid)
            if episode_by_compartment[comp]+'/'+event in timing_exclusions:excluded_ids.add(sid)
    jobs={jid:status for jid,sid,status in privacy.execute('SELECT id,source_id,status FROM permission_jobs') if sid in source_ids}
    timings=[(jid,sec) for jid,sec in memory.execute("SELECT job_id,seconds FROM pipeline_timings WHERE stage='source_total'") if jid in jobs]
    excluded_jobs={jid for jid,sid in privacy.execute('SELECT id,source_id FROM permission_jobs') if sid in excluded_ids}
    durations=[sec for jid,sec in timings if jid not in excluded_jobs]
    attempts=[json.loads(v or '{}') for jid,v in privacy.execute('SELECT job_id,validation FROM permission_attempts') if jid in jobs]
    memory.close();privacy.close()
    assert len(timings)==len(source_ids), (len(timings),len(source_ids))
    return {'metrics':metrics,'checkpoints_with_evidence':sum(bool(p['input_manifest']['memory_ids']) for p in predictions),
            'source_turns':len(source_ids),'timed_source_turns':len(durations),
            'timing_excluded_source_turns':len(excluded_ids),
            'quarantined_sources':sum(s=='quarantined' for s in jobs.values()),
            'semantic_calls_per_source':sum(a.get('semantic_response',True) for a in attempts)/len(jobs),
            'source_latency_seconds':{'mean':statistics.mean(durations),'median':statistics.median(durations),
                'p90':sorted(durations)[max(0,__import__('math').ceil(.9*len(durations))-1)],'total':sum(durations)},
            'predictions':[{'checkpoint_id':p['checkpoint_id'],'action':p['output']['action'],
                            'evidence_count':len(p['input_manifest']['memory_ids'])} for p in predictions]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,default=Path('runs/gatemem-medical-raw-stream-v15-openrouter-full-20261031T180100+0800'))
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--count',type=int,default=25)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--exclude-timing-turn',action='append',default=[],metavar='EPISODE_ID/TURN_ID',
        help='Apply the same explicitly documented timing exclusion to both runs; quality scores stay unchanged')
    args=p.parse_args()
    bp=rows(args.baseline/'official_predictions.jsonl')[:args.count]
    cp=rows(args.candidate/'official_predictions.jsonl')[:args.count]
    assert len(bp)==len(cp)==args.count
    assert [p['checkpoint_id'] for p in bp]==[p['checkpoint_id'] for p in cp], 'Different chronological checkpoint prefixes'
    bm=json.loads((args.baseline/'manifest.json').read_text());cm=json.loads((args.candidate/'manifest.json').read_text())
    for model in ['embedding','memory','judge']:assert bm['models'][model]==cm['models'][model],f'Different {model} models'
    assert bm['dataset_digest']==cm['dataset_digest']
    assert cm['initial_requester_grants']==0 and cm['opaque_model_identities']
    result={'checkpoint_count':args.count,'same_checkpoint_order':True,'same_models':True,'same_dataset':True,
            'recorded_provider_routing':{'baseline':bm.get('provider_routing'),'candidate':cm.get('provider_routing')},
            'timing_exclusions':args.exclude_timing_turn,
            'baseline_run':str(args.baseline.resolve()),'candidate_run':str(args.candidate.resolve()),
            'baseline':measured(args.baseline,bp,rows(args.baseline/'scores.jsonl'),args.exclude_timing_turn),
            'candidate':measured(args.candidate,cp,rows(args.candidate/'scores.jsonl'),args.exclude_timing_turn)}
    assert result['baseline']['timed_source_turns']==result['candidate']['timed_source_turns']
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:({a:b for a,b in v.items() if a!='predictions'} if isinstance(v,dict) else v)
                      for k,v in result.items()},indent=2))


if __name__=='__main__':main()
